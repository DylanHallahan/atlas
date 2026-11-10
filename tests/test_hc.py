"""The hc source: hc agents are recorded, a failing hc read keeps the last agents, and argv is never read or stored. The hc
binary is a stand-in script that prints a hand-written fixture with no argv in it, except that every terminal carries an argv
field with a marker, as does the `get` result (top level and foreground), to show it goes nowhere."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from types import SimpleNamespace  # noqa: E402

from atlas import db, hc, herdr, web  # noqa: E402
from atlas.structure import Structure  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
MARK = "ARGV-MARK-do-not-store"
HC = {"hidden_finished": 0, "terminals": [
    {"address": "t3", "agent": "claude-code", "cwd": "/x/finance", "name": "finance-lead", "state": "live", "status": "done", "turn": 1, "where": "a / 1"},
    {"address": "t4", "agent": "codex", "cwd": "/x/devops", "name": "devops", "state": "live", "status": "working", "turn": 0, "where": "a / 2"},
    {"address": "t5", "agent": "env", "cwd": "/x", "name": "a-shell", "state": "live", "turn": 0, "where": "a / 3"}]}


def script(folder, name, body):
    p = Path(folder) / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


class HcSource(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        path = os.path.join(self.t.name, "atlas.db")
        db.create(path)
        self.conn = db.connect(path)
        Path(self.t.name, "hc.json").write_text(json.dumps({**HC, "terminals": [{**x, "argv": [MARK]} for x in HC["terminals"]]}))
        get = json.dumps({"name": "x", "labels": {"herdr.space": "data-team", "team": "t"}, "argv": [MARK], "foreground": {"argv": [MARK]}})
        Path(self.t.name, "get.json").write_text(get)
        self.hc = script(self.t.name, "hc", f'''case "$1" in ls) cat {self.t.name}/hc.json;; get) echo "$2" >> {self.t.name}/gets; cat {self.t.name}/get.json;; send|keys) printf '%s\\n' "$@" > {self.t.name}/sent;; *) echo "screen of $4";; esac\n''')
        self.poller = herdr.StatePoller(self.conn, Structure(REPO / "structure.toml"), self.hc)

    def rows(self):
        return {r["name"]: dict(r) for r in self.conn.execute("SELECT * FROM agents")}

    def test_hc_agents_are_recorded_and_a_shell_is_left_out(self):
        self.poller.poll()
        rows = self.rows()
        self.assertEqual(set(rows), {"finance-lead", "devops"})
        self.assertEqual((rows["finance-lead"]["pane"], rows["finance-lead"]["runtime"], rows["finance-lead"]["status"], rows["finance-lead"]["cwd"]),
                         ("hc:finance-lead", "claude", "done", "/x/finance"))
        self.assertEqual((rows["devops"]["runtime"], rows["devops"]["status"]), ("codex", "working"))
        self.assertEqual({r["pane"] for r in herdr.live_panes(self.conn)}, {"hc:finance-lead", "hc:devops"})

    def test_space_is_read_once_per_name_and_only_the_label_is_kept(self):
        self.poller.poll()
        self.poller.poll()
        self.assertEqual({r["workspace"] for r in self.rows().values()}, {"data-team"})
        self.assertEqual(sorted(Path(self.t.name, "gets").read_text().split()), ["devops", "finance-lead"])
        self.assertEqual(hc.space(self.hc, "x"), "data-team")
        Path(self.t.name, "get.json").write_text("not json")
        self.assertIsNone(hc.space(self.hc, "x"))

    def test_a_failing_read_keeps_the_last_agents_and_says_so(self):
        self.poller.poll()
        Path(self.hc).write_text("#!/bin/sh\nexit 1\n")
        self.poller.poll()
        self.assertEqual(set(self.rows()), {"finance-lead", "devops"})
        self.assertEqual(self.conn.execute("SELECT state FROM source_health WHERE source = 'hc'").fetchone()[0], "failing")

    def sent(self, body, status="idle"):
        """What the endpoint's own send code (send_target, then hc.send) runs against the stand-in hc."""
        self.poller.poll()
        self.conn.execute("UPDATE agents SET status = ? WHERE name = 'finance-lead'", (status,))
        self.conn.execute("INSERT OR REPLACE INTO reports (path, team, name, in_reports, mtime_ns, size, has_change, has_ask, linked) VALUES ('/r', 'finance', 'ask', 1, 0, 0, 0, 1, 1)")
        app = SimpleNamespace(hc=self.hc, structure=self.poller.structure)
        Path(self.t.name, "sent").unlink(missing_ok=True)
        fields = {}
        hc.send(self.hc, *web.send_target(self.conn, app, {"agent": "finance-lead", **body}, fields))
        return Path(self.t.name, "sent").read_text().split("\n")[:-1]

    def test_send_runs_hc_send_and_hc_keys_with_options_ended(self):
        self.assertEqual(self.sent({"kind": "line", "text": "-n hello"}), ["send", "--", "finance-lead", "-n hello"])
        self.assertEqual(self.sent({"kind": "answer", "text": "go", "ask": "finance/ask", "pick": "A"}), ["send", "--", "finance-lead", "go"])
        self.assertEqual(self.sent({"kind": "key", "key": "Shift+Tab"}), ["keys", "--", "finance-lead", "shift+tab"])
        self.assertEqual(self.sent({"kind": "char", "key": "-"}), ["keys", "--", "finance-lead", "-"])
        self.assertEqual(self.sent({"kind": "char", "key": " "}), ["keys", "--", "finance-lead", "space"])

    def test_send_refusals(self):
        with self.assertRaises(web.Refused) as r:
            self.sent({"kind": "char", "key": "A"})
        self.assertEqual((r.exception.reason, r.exception.audited), ("bad-body", "char"))
        with self.assertRaises(web.Refused) as r:
            self.sent({"kind": "line", "text": "hi"}, status="blocked")
        self.assertEqual(r.exception.reason, "blocked")
        # hc failing: the agent is not in a successful read, so it is gone
        Path(self.hc).write_text("#!/bin/sh\nexit 1\n")
        self.poller.poll()
        with self.assertRaises(web.Refused) as r:
            web.send_target(self.conn, SimpleNamespace(hc=self.hc, structure=Structure(REPO / "structure.toml")),
                            {"agent": "finance-lead", "kind": "key", "key": "Enter"}, {})
        self.assertEqual(r.exception.reason, "gone")
        for command in ("get", "ls", "kill", "rm"):
            with self.assertRaises(ValueError):
                hc.send(self.hc, command, "x", "y")

    def test_a_send_to_the_private_agent_is_refused_and_runs_no_command(self):
        terminals = [*HC["terminals"], {"address": "t9", "agent": "claude-code", "cwd": "/x", "name": db.PRIVATE, "state": "live", "status": "idle", "turn": 0, "where": "a / 9"}]
        Path(self.t.name, "hc.json").write_text(json.dumps({**HC, "terminals": terminals}))
        for body in ({"kind": "line", "text": "hi"}, {"kind": "key", "key": "Enter"}):
            with self.assertRaises(web.Refused) as r:
                self.sent({**body, "agent": db.PRIVATE})
            self.assertEqual((r.exception.code, r.exception.reason), (400, "gone"))
            self.assertFalse(Path(self.t.name, "sent").exists())

    def test_screen_without_hcs_incomplete_line(self):
        self.hc = script(self.t.name, "hc2", 'echo one; echo "[hc] incomplete: older output may be missing"; echo two\n')
        self.assertEqual(hc.read(self.hc, "x"), "one\ntwo\n")

    def test_argv_is_never_stored_and_hc_only_reads(self):
        self.poller.poll()
        herdr.ScreensPoller(self.conn, self.hc).poll()
        self.assertEqual(self.conn.execute("SELECT text FROM screens WHERE pane = 'hc:devops'").fetchone()[0], "screen of devops\n")
        for (table,) in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            for row in self.conn.execute(f'SELECT * FROM "{table}"'):
                self.assertNotIn(MARK, repr(tuple(row)))
        for command in ("send", "keys", "kill", "rm", "label", "interrupt", "attach", "server", "update"):
            with self.assertRaises(ValueError):
                hc.run(self.hc, command, "x")


if __name__ == "__main__":
    unittest.main()
