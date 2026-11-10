"""Each Claude account's usage: percent used and reset, per limit window, read from Anthropic when a page asks.

Atlas's one credential and its one call off the box (design note 2026-10-31-claude-usage-read-design-note). The token
is read from one of two fixed files, held in a local variable for one request, and never stored, cached, logged or
shown. Atlas never refreshes a token and never writes a credential file. Only parsed figures are cached."""
import http.client
import json
import os
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import db
from .files import open_below, read_fd

# The only credential files Atlas opens, from a fixed list: (key, name, profile folder under home).
ACCOUNTS = (("work", "Work", ".claude-org"), ("personal", "Personal", ".claude"))
HOME = Path.home()
HOST, PORT, PATH = "api.anthropic.com", 443, "/api/oauth/usage?skip_spend=1"
BETA = "oauth-2025-04-20"
AGENT = "Atlas (usage read)"
TTL, TIMEOUT, CAP = 300, 5, 64 * 1024
SIGN_IN = "the sign-in needs Claude Code to refresh it"
LABELS = {"five_hour": "5-hour", "seven_day": "Weekly", "seven_day_opus": "Weekly, Opus"}

STATE, LOCKS = {}, {key: threading.Lock() for key, _, _ in ACCOUNTS}


def context():
    """TLS with verification on. A check instance replaces this to trust its stand-in's certificate."""
    return ssl.create_default_context()


def log(name, status, started):
    """One line per call: the account, the HTTP status (or what failed) and the duration. Nothing else."""
    print(f"usage: {name} {status} {round((time.monotonic() - started) * 1000)} ms", file=sys.stderr, flush=True)


def stamp(folder):
    """The credential file's mtime in nanoseconds, from metadata only: the file is opened with no symlink followed and
    never read. None when it is absent or not a plain file."""
    try:
        fd = open_below(HOME, f"{folder}/.credentials.json")
    except OSError:
        return None
    try:
        return os.fstat(fd).st_mtime_ns
    finally:
        os.close(fd)


def credential(folder):
    """(token, expiresAt, mtime) from <home>/<folder>/.credentials.json, opened with no symlink followed. Only those
    two values are kept. (None, None, None) when the file is absent or unreadable; a missing token is None too."""
    try:
        fd = open_below(HOME, f"{folder}/.credentials.json")
        mtime = os.fstat(fd).st_mtime_ns
        found = json.loads(read_fd(fd, CAP)).get("claudeAiOauth")
        token, expires = found.get("accessToken"), found.get("expiresAt")
    except (OSError, ValueError, AttributeError):
        return None, None, None
    return (token if isinstance(token, str) and token else None), expires, mtime


def expired(expires):
    """True when expiresAt (milliseconds since the epoch) has passed. An unreadable value counts as expired."""
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return True
    return expires / (1000 if expires > 1e11 else 1) <= time.time()


def parse(body):
    """The windows of a response, strictly: each known window key with a number utilization from 0 to 100 and an ISO
    resets_at; anything else is skipped. None when no window is left, so a changed shape never gives a wrong number."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    out = []
    for key, w in data.items() if isinstance(data, dict) else ():
        used, at = (w.get("utilization"), w.get("resets_at")) if isinstance(w, dict) else (None, None)
        if key not in LABELS or isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100:
            continue
        try:
            at = datetime.fromisoformat(at.replace("Z", "+00:00")) if isinstance(at, str) else None
        except ValueError:
            at = None
        if at and at.tzinfo:
            at = at.isoformat()
            out.append({"key": key, "label": LABELS[key], "percent": used, "resets_at": at})
    return out or None


def fetch(token):
    """(status, windows or a reason). The one request: HTTPS to the fixed host, verified, no redirect followed, no proxy
    (http.client reads no proxy variable), 5 s per step, at most 64 KB. Never raises, so no error can carry the token:
    a failure is reported by its class name and nothing else."""
    status = "error"
    try:
        conn = http.client.HTTPSConnection(HOST, PORT, timeout=TIMEOUT, context=context())
        try:
            conn.request("GET", PATH, headers={"Authorization": f"Bearer {token}", "anthropic-beta": BETA,
                                               "User-Agent": AGENT, "Accept": "application/json"})
            res = conn.getresponse()
            status = res.status
            if status in (401, 403):
                return status, SIGN_IN  # decided from the status line; a body that stalls cannot change it
            body = res.read(CAP + 1)
        finally:
            conn.close()
        if status != 200 or len(body) > CAP:
            return status, "Anthropic did not answer the read"
        windows = parse(body)
        return status, windows or "Anthropic answered in a shape Atlas does not know"
    except Exception as e:  # noqa: BLE001  # the class name only, never the message
        return type(e).__name__ if status == "error" else status, "Anthropic could not be reached"


def unknown(reason, last):
    return {"state": "unknown", "reason": reason, "windows": [], "last": last, "read_at": db.now()}


def current(answer):
    """The answer as it holds now. A window past its reset is no longer a current figure: it moves to `last`, greyed and
    dated; with no window left the answer is unknown. Decided here on every answer, a cache hit too."""
    if answer["state"] != "ok":
        return answer
    now = datetime.now().astimezone()
    live = [w for w in answer["windows"] if datetime.fromisoformat(w["resets_at"]) > now]
    if len(live) == len(answer["windows"]):
        return answer
    old = {"windows": [w for w in answer["windows"] if w not in live], "read_at": answer["read_at"]}
    if live:
        return answer | {"windows": live, "last": old}
    return unknown("the window has reset since the last read", old) | {"read_at": answer["read_at"]}


def read(key, name, folder):
    """One account's answer: its windows with percent and reset, or unknown with a reason. At most one call in flight
    per account; the answer, a failure too, is cached for 5 minutes, and a sign-in failure is not tried again until the
    credential file changes."""
    with LOCKS[key]:
        old = STATE.get(key)
        mtime = stamp(folder)
        if old and old["mtime"] == mtime:
            held = old["answer"]["state"] == "unknown" and old["answer"]["reason"] == SIGN_IN
            if held or time.monotonic() - old["at"] < TTL:
                return current(old["answer"])
        token, expires, _ = credential(folder)
        last = old and (old["answer"] if old["answer"]["state"] == "ok" else old["answer"]["last"])
        started = time.monotonic()
        try:
            if token is None:
                log(name, "no credential", started)
                answer = unknown("no sign-in found for this account", last)
            elif expired(expires):
                log(name, "expired, not called", started)
                answer = unknown(SIGN_IN, last)
            else:
                status, result = fetch(token)
                log(name, status, started)
                answer = ({"state": "ok", "windows": result, "last": None, "read_at": db.now()} if isinstance(result, list)
                          else unknown(result, last))
        finally:
            token = None
        STATE[key] = {"at": time.monotonic(), "mtime": mtime, "answer": answer}
        return current(answer)


def get(app=None):
    """/api/usage: every account's answer. Takes no input."""
    with ThreadPoolExecutor(len(ACCOUNTS)) as pool:
        answers = list(pool.map(lambda a: read(*a), ACCOUNTS))
    return {"as_of": db.now(), "accounts": [{"key": k, "name": n} | a for (k, n, _), a in zip(ACCOUNTS, answers)]}
