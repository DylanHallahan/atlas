"""The token check (design note 2026-10-31, atlas-lead's addition): no API route, log line, error message or database
byte can carry the account's token, whatever the call does. A fake credential file holds a recognisable fake token and a
local HTTPS stand-in answers in place of Anthropic. Run: python3 -m unittest discover -s tests"""
import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atlas import db, review, usage, web  # noqa: E402
from atlas.structure import Structure  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TOKEN = "sk-ant-oat01-FAKE-recognisable-token-0123456789-do-not-leak"
GOOD = {"five_hour": {"utilization": 86.0, "resets_at": "2099-01-01T18:00:00.000000+00:00"},
        "seven_day": {"utilization": 12, "resets_at": "2099-01-05T15:00:00Z"},
        "seven_day_opus": {"utilization": 3, "resets_at": "2099-01-05T15:00:00Z"},
        "extra_usage": {"utilization": 99, "resets_at": "2099-01-05T15:00:00Z"}, "junk": 5}


class StandIn(BaseHTTPRequestHandler):
    mode, seen = "ok", []

    def do_GET(self):
        StandIn.seen.append((self.path, self.headers.get("Authorization"), self.headers.get("anthropic-beta")))
        mode = StandIn.mode
        if mode == "timeout":
            time.sleep(1.5)
        if mode == "redirect" and self.path.startswith("/api/oauth/usage"):
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()
            return
        code = {"401": 401, "500": 500}.get(mode, 200)
        body = {"malformed": b"{not json", "huge": b" " * (usage.CAP + 10), "shape": b'{"five_hour": {"utilization": "86"}}',
                "401": b'{"error": "bad token"}', "500": b"oops"}.get(mode) or json.dumps(GOOD).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *args):
        pass


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = Path(cls.tmp.name)
        pem = t / "standin.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(pem), "-out", str(pem),
                        "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                       check=True, capture_output=True)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(pem)
        cls.standin = ThreadingHTTPServer(("127.0.0.1", 0), StandIn)
        cls.standin.socket = tls.wrap_socket(cls.standin.socket, server_side=True)
        threading.Thread(target=cls.standin.serve_forever, daemon=True).start()
        client = ssl.create_default_context(cafile=str(pem))
        cls.patches = [mock.patch.object(usage, "HOME", t / "home"), mock.patch.object(usage, "HOST", "localhost"),
                       mock.patch.object(usage, "PORT", cls.standin.server_address[1]),
                       mock.patch.object(usage, "TIMEOUT", 0.5), mock.patch.object(usage, "context", lambda: client)]
        for p in cls.patches:
            p.start()
        cls.creds = {}
        for _, _, folder in usage.ACCOUNTS:
            f = t / "home" / folder / ".credentials.json"
            f.parent.mkdir(parents=True)
            f.write_text(json.dumps({"claudeAiOauth": {"accessToken": TOKEN, "refreshToken": "REFRESH-" + TOKEN,
                                                      "expiresAt": int((time.time() + 3600) * 1000)}}))
            cls.creds[folder] = f
        cls.db = str(t / "atlas.db")
        db.create(cls.db)
        app = SimpleNamespace(db=cls.db, structure=Structure(REPO / "structure.toml"), herdr="/nonexistent-herdr", mocks=None,
                              typist=None, devices=set(), serve_uid=0, commit="test", deploy="test")
        app.review = review.Review(db.connect(cls.db), app.structure)
        web.Handler.app, web.Handler.files = app, web.static_files()
        cls.web = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=cls.web.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.web.shutdown()
        cls.standin.shutdown()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def setUp(self):
        usage.STATE.clear()
        StandIn.mode, StandIn.seen = "ok", []
        self.err = StringIO()
        self.stderr = mock.patch.object(sys, "stderr", self.err)
        self.stderr.start()
        self.addCleanup(self.stderr.stop)

    def get(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.web.server_address[1]}{path}", timeout=20) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
        except http.client.RemoteDisconnected:  # a route that raised: its traceback is in the log, which is swept too
            return 0, ""

    def swept(self):
        """Every /api route's body, the log, the database and its WAL, as one text."""
        out = [self.get(p)[1] for p in [*web.ROUTES, "/api/whoami", "/api/briefing", "/api/review", "/api/shipped-picture"]]
        out.append(self.err.getvalue())
        for suffix in ("", "-wal", "-shm"):
            f = Path(self.db + suffix)
            out.append(f.read_bytes().decode("latin-1") if f.exists() else "")
        return "\n".join(out)

    def no_token(self):
        text = self.swept()
        for secret in (TOKEN, "REFRESH-"):
            self.assertNotIn(secret, text)
        self.assertNotIn(TOKEN[:12], text)

    # ---- what the stand-in answers ----

    def test_good_read_parses_only_known_windows(self):
        status, body = self.get("/api/usage")
        self.assertEqual(status, 200)
        for a in json.loads(body)["accounts"]:
            self.assertEqual(a["state"], "ok")
            self.assertEqual([(w["key"], w["percent"]) for w in a["windows"]],
                             [("five_hour", 86.0), ("seven_day", 12), ("seven_day_opus", 3)])
        self.assertEqual([x[1] for x in StandIn.seen], [f"Bearer {TOKEN}"] * 2)  # the call itself does carry it
        self.assertEqual({x[2] for x in StandIn.seen}, {"oauth-2025-04-20"})
        self.assertEqual(len(self.err.getvalue().strip().splitlines()), 2)  # one log line per call
        self.assertRegex(self.err.getvalue(), r"usage: Work 200 \d+ ms")
        self.no_token()

    def test_each_failure_is_unknown_and_leaks_nothing(self):
        for mode in ("timeout", "401", "500", "redirect", "malformed", "huge", "shape"):
            with self.subTest(mode=mode):
                usage.STATE.clear()
                StandIn.mode, StandIn.seen = mode, []
                status, body = self.get("/api/usage")
                self.assertEqual(status, 200)
                for a in json.loads(body)["accounts"]:
                    self.assertEqual((a["state"], a["windows"]), ("unknown", []))
                    self.assertTrue(a["reason"])
                if mode == "redirect":
                    self.assertEqual({p for p, _, _ in StandIn.seen}, {usage.PATH})  # never followed
                self.no_token()

    def test_exception_inside_the_request_carries_no_token(self):
        def boom(*args, **kwargs):
            raise RuntimeError(f"boom {TOKEN} {args!r} {kwargs!r}")

        def boom_off_main(*args, **kwargs):  # the test's own client reads responses on the main thread
            if threading.current_thread() is threading.main_thread():
                return real_read(*args, **kwargs)
            return boom(*args, **kwargs)
        real_read = http.client.HTTPResponse.read
        for owner, name, fake in ((http.client.HTTPSConnection, "request", boom), (http.client.HTTPSConnection, "getresponse", boom),
                                  (http.client.HTTPResponse, "read", boom_off_main)):
            with self.subTest(target=name):
                usage.STATE.clear()
                with mock.patch.object(owner, name, fake):
                    body = self.get("/api/usage")[1]
                for a in json.loads(body)["accounts"]:
                    self.assertEqual(a["state"], "unknown")
                self.no_token()
        usage.STATE.clear()
        with mock.patch.object(usage, "parse", boom):  # after the response, in the parser
            self.assertEqual(json.loads(self.get("/api/usage")[1])["accounts"][0]["state"], "unknown")
        self.no_token()

    def test_fetch_never_raises_and_names_no_message(self):
        with mock.patch.object(http.client.HTTPSConnection, "request", side_effect=OSError(TOKEN)):
            result = usage.fetch(TOKEN)
        self.assertNotIn(TOKEN, repr(result))
        self.assertEqual(result, ("OSError", "Anthropic could not be reached"))

    # ---- the rules around the call ----

    def test_cache_holds_five_minutes_and_one_call_in_flight(self):
        self.get("/api/usage")
        self.get("/api/usage")
        self.assertEqual(len(StandIn.seen), 2)  # two accounts, one call each
        self.assertNotIn(TOKEN, json.dumps(usage.STATE))  # parsed figures only
        usage.STATE["work"]["at"] -= usage.TTL + 1
        self.get("/api/usage")
        self.assertEqual(len(StandIn.seen), 3)
        usage.STATE.clear()
        StandIn.seen.clear()
        threads = [threading.Thread(target=usage.read, args=usage.ACCOUNTS[0]) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(StandIn.seen), 1)

    def test_a_401_is_not_retried_until_the_file_changes(self):
        StandIn.mode = "401"
        self.get("/api/usage")
        n = len(StandIn.seen)
        for a in usage.STATE.values():
            a["at"] -= usage.TTL + 1
        a = json.loads(self.get("/api/usage")[1])["accounts"][0]
        self.assertEqual((len(StandIn.seen), a["reason"]), (n, usage.SIGN_IN))
        f = self.creds[".claude-org"]
        os.utime(f, ns=(time.time_ns(), time.time_ns() + 10**9))
        StandIn.mode = "ok"
        self.assertEqual(json.loads(self.get("/api/usage")[1])["accounts"][0]["state"], "ok")

    def test_an_expired_token_is_never_sent(self):
        f = self.creds[".claude"]
        before = f.read_text()
        f.write_text(json.dumps({"claudeAiOauth": {"accessToken": TOKEN, "expiresAt": 1000}}))
        self.addCleanup(f.write_text, before)
        a = {x["key"]: x for x in json.loads(self.get("/api/usage")[1])["accounts"]}
        self.assertEqual((a["personal"]["state"], a["personal"]["reason"]), ("unknown", usage.SIGN_IN))
        self.assertEqual(len(StandIn.seen), 1)  # only the work account was called
        self.no_token()

    def test_a_missing_credential_is_unknown_and_a_symlink_is_refused(self):
        f = self.creds[".claude"]
        saved = f.read_text()
        f.unlink()
        try:
            self.assertIn("no sign-in", json.loads(self.get("/api/usage")[1])["accounts"][1]["reason"])
            real = f.parent / "real.json"
            real.write_text(saved)
            f.symlink_to(real)
            usage.STATE.clear()
            self.assertEqual(json.loads(self.get("/api/usage")[1])["accounts"][1]["state"], "unknown")
        finally:
            f.unlink(missing_ok=True)
            f.write_text(saved)

    def test_proxy_variables_are_ignored_and_credential_files_are_not_written(self):
        before = {k: (f.read_bytes(), f.stat().st_mtime_ns) for k, f in self.creds.items()}
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
                                          "ALL_PROXY": "http://127.0.0.1:9"}):
            self.assertEqual(json.loads(self.get("/api/usage")[1])["accounts"][0]["state"], "ok")
        self.assertEqual(before, {k: (f.read_bytes(), f.stat().st_mtime_ns) for k, f in self.creds.items()})
        self.assertFalse([p for p, _, _ in StandIn.seen if "token" in p])  # never a refresh


if __name__ == "__main__":
    unittest.main()
