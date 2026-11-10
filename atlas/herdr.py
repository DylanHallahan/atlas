"""hc pollers. They read only: `hc ls` for state and `hc read` for screens. The herdr `run` stays for POST /api/send in web.py
(`agent prompt`, `agent send-keys`), for the typist only; every agent is an hc agent now, so no send finds a target."""
import json
import subprocess
import time
from datetime import timedelta

from . import db, hc

TIMEOUT = 5


def run(herdr, *args):
    """Run one herdr command, argv only, and return its stdout. A missing binary, a timeout or a non-zero exit is a RuntimeError."""
    try:
        p = subprocess.run([herdr, *args], capture_output=True, text=True, errors="replace", timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"herdr {args[0]} {args[1]}: timed out after {TIMEOUT} s")
    except OSError as e:
        raise RuntimeError(f"herdr: {e}")
    if p.returncode != 0:
        raise RuntimeError(f"herdr {args[0]} {args[1]}: exit {p.returncode}: {(p.stderr or p.stdout).strip()[:200]}")
    return p.stdout


def every(interval, poll):
    """Call poll every interval seconds, start to start, for the life of the process."""
    while True:
        started = time.monotonic()
        try:
            poll()
        except Exception as e:  # a poller thread never dies; source health shows the failures it can name
            print(f"{poll.__qualname__}: {e!r}", flush=True)
        time.sleep(max(0.0, interval - (time.monotonic() - started)))


class StatePoller:
    interval = 2

    def __init__(self, conn, structure, hc_binary="hc"):
        self.conn, self.hc, self.structure = conn, hc_binary, structure
        # A status change is dated exactly only when the previous snapshot also succeeded.
        # False at start, so after a wipe every status already set is "since at least".
        self.last_ok = False

    def poll(self):
        """Read `hc ls` and write the agents in one transaction. hc's snapshot numbers are negative, so no row stored
        from herdr's old numbering ever counts as live. A failed read, or a failure processing what it returned, is
        rolled back and changes no agent row."""
        self.structure.refresh(self.conn)
        at = db.now()
        try:
            agents = hc.agents(self.hc)
            with self.conn:
                # Membership is by sequence number, not timestamp: two snapshots can fall in the same second.
                (snapshot,) = self.conn.execute(
                    "SELECT -coalesce(max(abs(snapshot)), 0) - 1 FROM source_health WHERE source = 'hc'"
                ).fetchone()
                for a in agents:
                    self.record(a, at, snapshot)
                # A worker leaves after 24 hours Gone; a declared agent never does.
                self.conn.execute(
                    "DELETE FROM agents WHERE last_seen < ? AND name NOT IN (SELECT value FROM json_each(?))",
                    (db.now(timedelta(hours=24)), json.dumps(list(self.structure.declared()))),
                )
                db.set_health(self.conn, "hc", at)
                # herdr is no longer read. Its old row would keep its last rows live in the send and review joins.
                self.conn.execute("DELETE FROM source_health WHERE source = 'herdr'")
                self.conn.execute("UPDATE source_health SET snapshot = ? WHERE source = 'hc'", (snapshot,))
        except Exception as e:
            self.last_ok = False
            with self.conn:
                db.set_health(self.conn, "hc", at, "failing", str(e) if isinstance(e, RuntimeError) else f"snapshot: {e!r}")
            return
        self.last_ok = True

    def record(self, a, at, snapshot):
        name, pane, status = a.get("name") or a.get("pane_id"), a.get("pane_id"), a.get("agent_status")
        if not name:
            return
        # hc gives no session id outside argv, which Atlas never reads: the session is the one the registry (`--name`)
        # last saw under the agent's name.
        session = (self.conn.execute(
            "SELECT session_id FROM session_names WHERE registry_name = ? ORDER BY last_seen DESC LIMIT 1", (name,)
        ).fetchone() or (None,))[0]
        old = self.conn.execute(
            "SELECT status, pane, session_id, state_since, state_since_exact, workspace FROM agents WHERE name = ?", (name,)
        ).fetchone()
        # The space (its team) is read once per new name: an hc row that already has one keeps it.
        workspace = old["workspace"] if old and old["pane"] == pane and old["workspace"] else hc.space(self.hc, name)
        if old and (old["status"], old["pane"], old["session_id"]) == (status, pane, session):
            since, exact = old["state_since"], old["state_since_exact"]
        else:
            since, exact = at, int(self.last_ok)
        self.conn.execute(
            """INSERT INTO agents (name, pane, tab, workspace, runtime, status, session_id, state_since,
                                   state_since_exact, cwd, terminal_title, last_seen, snapshot)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (name) DO UPDATE SET
                   pane = excluded.pane, tab = excluded.tab, workspace = excluded.workspace,
                   runtime = excluded.runtime, status = excluded.status, session_id = excluded.session_id,
                   state_since = excluded.state_since, state_since_exact = excluded.state_since_exact,
                   cwd = excluded.cwd, terminal_title = excluded.terminal_title, last_seen = excluded.last_seen,
                   snapshot = excluded.snapshot, rebuilt = NULL, name_lost = NULL,
                   -- a rule 1 placement belongs to the session it was found for; a new session behind the name drops it
                   placement_rule = CASE WHEN agents.session_id IS excluded.session_id THEN agents.placement_rule END,
                   placed_by = CASE WHEN agents.session_id IS excluded.session_id THEN agents.placed_by END,
                   placed_by_key = CASE WHEN agents.session_id IS excluded.session_id THEN agents.placed_by_key END,
                   placed_session = CASE WHEN agents.session_id IS excluded.session_id THEN agents.placed_session END,
                   -- so is a now line
                   now = CASE WHEN agents.session_id IS excluded.session_id THEN agents.now END,
                   now_at = CASE WHEN agents.session_id IS excluded.session_id THEN agents.now_at END,
                   account = CASE WHEN agents.session_id IS excluded.session_id THEN agents.account END,
                   model = CASE WHEN agents.session_id IS excluded.session_id THEN agents.model END""",
            (name, pane, a.get("tab_id"), workspace, a.get("agent"), status, session, since, exact,
             a.get("cwd"), None if name == db.PRIVATE else a.get("terminal_title_stripped") or a.get("terminal_title"), at, snapshot),
        )
        if session:
            self.conn.execute(
                """INSERT INTO session_names (session_id, herdr_name, pane, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (session_id, herdr_name, pane) WHERE herdr_name IS NOT NULL
                   DO UPDATE SET last_seen = excluded.last_seen""",
                (session, name, pane or "", at, at),
            )


def live_panes(conn):
    """Panes in hc's last successful snapshot, as "hc:<name>", with their names."""
    return conn.execute(
        """SELECT a.pane, a.name FROM agents a JOIN source_health h ON h.source = 'hc' AND a.snapshot = h.snapshot
           WHERE a.pane IS NOT NULL ORDER BY a.pane"""
    ).fetchall()


def hc_ok(conn):
    row = conn.execute("SELECT state FROM source_health WHERE source = 'hc'").fetchone()
    return row is not None and row["state"] == "ok"


class ScreensPoller:
    interval = 4

    def __init__(self, conn, hc_binary="hc"):
        self.conn, self.hc = conn, hc_binary

    def poll(self):
        ok = hc_ok(self.conn)
        live = [r for r in live_panes(self.conn) if r["name"] != db.PRIVATE] if ok else []
        screens, errors = [], []
        for pane, name in live:
            try:
                screens.append((pane, name, hc.read(self.hc, name), db.now()))
            except RuntimeError as e:
                errors.append(f"{pane}: {e}")
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            # While the hc read fails, before or during the reads, screens keep their last rows and read times.
            if not (ok and hc_ok(self.conn)):
                db.set_health(self.conn, "screens", db.now(), "failing", "held: hc read failing")
                return
            # Only panes in the current snapshot are written; a failed read leaves that pane's last screen in place.
            current = [pane for pane, _ in live_panes(self.conn)]
            self.conn.executemany(
                """INSERT INTO screens (pane, herdr_name, text, read_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT (pane) DO UPDATE SET
                       herdr_name = excluded.herdr_name, text = excluded.text, read_at = excluded.read_at""",
                [s for s in screens if s[0] in current],
            )
            self.conn.execute(
                "DELETE FROM screens WHERE pane NOT IN (SELECT value FROM json_each(?))", (json.dumps(current),)
            )
            at = db.now()
            if not errors:
                db.set_health(self.conn, "screens", at)
            else:
                state = "failing" if len(errors) == len(live) else "degraded"
                more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
                db.set_health(self.conn, "screens", at, state, errors[0] + more, len(errors))
