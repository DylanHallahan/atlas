"""HTTP on loopback: the home page at /, the Teams chart at /office, an agent's terminal at /agent/<name>, an ask at
/ask/<team>/<name>, a change at /change/<team>/<name>, the list of changes at /changes, the review queue at /review, each Claude account's usage at /usage, a change's code at /pr/<repo>/<n>, the files under /static/,
/api/state, /api/screens, /api/history, /api/messages, /api/whoami, /api/briefing, /api/shipped-picture, the reports routes
/api/changes, /api/change, /api/ask, /api/usage, /api/items, /api/item, /api/report-file and /api/upload (a stored screenshot, by id), POST /api/seen, POST /api/upload, and each team's pages under /a/<team>/ (/mocks/ redirects to /a/atlas/)."""
import gzip
import hashlib
import hmac
import contextlib
import copy
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from .structure import expand
from . import artifacts, db, fleet, hc, herdr, items, lineage, raincli, reports, review, status, transcripts, usage
from .messages import home as sender_home
from .review import NotFound
from .files import open_below, open_dir, open_root, read_fd
from .transcripts import iso, stamp

AGENT_COLUMNS = ("name", "pane", "tab", "workspace", "runtime", "status", "session_id", "state_since",
                 "state_since_exact", "cwd", "terminal_title", "last_seen", "snapshot", "placement_rule",
                 "placed_by", "placed_by_key", "placed_session", "rebuilt", "name_lost", "now", "now_at", "account", "model")

# herdr's agent_status to the page's word. done is idle that nobody has seen in herdr yet.
STATES = {"working": "working", "blocked": "blocked", "idle": "idle", "done": "idle", "unknown": "unknown"}


def place(a, structure, rows):
    """team, parent and placement_rule of an unlisted agent, from its stored cwd and workspace: first rule that applies.
    rows: every agent row by herdr name. A parent comes only from a message or a brief; a folder or workspace gives the
    team alone (parent-unknown)."""
    # Rule 1: the transcript poller stores the herdr name whose message first reached this agent's session. It places
    # the agent only for that session (herdr gives no session for a Codex pane), while that sender is on the graph, and
    # only inside the sender's own workspace: a message from another workspace is not a parent (the chief's Codex in w9
    # was placed under a Forecast lead that once wrote to it). The sender's workspace is its own row's, else its
    # declared team's.
    sender = structure.declared().get(a["placed_by"])
    home = sender_home(structure, rows, a["placed_by"])
    if (a["placement_rule"] == "message" and (sender or a["placed_by"] in rows) and a["placed_session"]
            and a["session_id"] in (None, a["placed_session"]) and home and a["workspace"] == home):
        team = sender.get("team") if sender else structure.team_by_folder(a["cwd"]) or structure.team_by_workspace(a["workspace"])
        return {"team": team, "parent": a["placed_by"], "placement_rule": "message"}
    # Rule "brief": the agent that wrote the brief its session's first prompt named, when that agent is on the graph.
    # settle() then checks it against everyone's final placement: the same team, and no cycle.
    team = structure.team_by_folder(a["cwd"]) or structure.team_by_workspace(a["workspace"])
    if (a["placement_rule"] == "brief" and a["placed_session"] and a["session_id"] in (None, a["placed_session"])
            and team and (sender or a["placed_by"] in rows)):
        return {"team": team, "parent": a["placed_by"], "placement_rule": "brief"}
    # Rules "launch" and "pairing" are worked out again on every poll, so they need no session to match; a Codex agent
    # has none. Each is checked like "brief" here, and by settle().
    if (a["placement_rule"] in ("launch", "pairing") and a["session_id"] in (None, a["placed_session"])
            and team and a["placed_by"] != a["name"] and (sender or a["placed_by"] in rows)):
        return {"team": team, "parent": a["placed_by"], "placement_rule": a["placement_rule"]}
    # A folder or workspace gives the team, never a parent: an unlisted agent is a worker, and its head is a guess.
    if team:
        return {"team": team, "parent": None, "placement_rule": "parent-unknown"}
    return {"team": None, "parent": None, "placement_rule": "unplaced"}


def settle(agents):
    """Every agent placed by brief, launch or pairing, checked against the others' final placements, in name order: its parent must be
    in its own team as placed (not as its folder suggests), and following parents up from it must never come back to
    it. Any that fails is demoted to parent-unknown, keeping its team. Demoting only removes a parent, so it never
    makes a new cycle or moves a team, and the order makes the result the same on every read."""
    by = {a["name"]: a for a in agents}
    for a in sorted((a for a in agents if a["placement_rule"] in ("brief", "launch", "pairing")), key=lambda a: a["name"]):
        p, steps = by.get(a["parent"]), 0
        ok = p is not None and p["team"] == a["team"]
        while ok and p is not None and p["parent"] and steps <= len(agents):
            ok, p, steps = p["parent"] != a["name"], by.get(p["parent"]), steps + 1
        if not ok:
            a |= {"parent": None, "placement_rule": "parent-unknown"}
    return agents


def view(app):
    """The app with the structure as it is now, for one request: the state poller's reload replaces the structure's
    data and never changes it in place, so a shallow copy is a view that a reload mid-request cannot change."""
    return SimpleNamespace(**vars(app) | {"structure": copy.copy(app.structure)})


def state(conn, app, query):
    """Every agent herdr has shown and every declared agent, once each, and team_order, the home page's team order.
    Gone is derived here, not stored."""
    app = view(app)
    with db.read(conn):
        sources, rows, _, asking = state_rows(conn)
        done = finished(conn)
        began = {r[0]: r[1] for r in conn.execute(
            """SELECT a.name, min(s.name_since) FROM agents a JOIN session_names s
               ON s.session_id = a.session_id AND s.registry_name = a.name WHERE s.name_since IS NOT NULL GROUP BY a.name""")}
        asks = conn.execute("SELECT count(*) FROM reports WHERE linked AND has_ask").fetchone()[0]
    out = compose(app, sources, rows)
    # Gone agents are most of the answer and only some pages read them: ?all=1 keeps every agent, ?agent=<name> that one as well.
    if query.get("all") != ["1"]:
        out["agents"] = [a for a in out["agents"] if a["gone"] is not True or a["name"] == query.get("agent", [None])[0]]
    for a in out["agents"]:
        # Finished: the agent's own report (<date>-<name>-report.md, in its team's reports folder) was written after its
        # current session took the name (the registry's name_since, which every transcript poll rewrites, so a re-index
        # keeps it), so a reused name does not fold. A session with no registry row (Codex) never folds.
        a["finished"] = any(t == a["team"] and m >= (began.get(a["name"]) or "9") for t, m in done.get(a["name"], []))
    with db.read(conn):
        rain = raincli.cards(conn)
    return out | {"team_order": team_order(app, asking), "asks": asks, "raincli": rain, "private": db.PRIVATE}


def finished(conn):
    """Each agent name to the (team, modified) of the reports named <date>-<name>-report, in any subfolder."""
    out = {}
    for r in conn.execute("SELECT team, name, modified FROM reports WHERE in_reports AND name LIKE '%-report'"):
        m = re.fullmatch(r"(?:.*/)?\d{4}-\d{2}-\d{2}-(.+)-report", r["name"])
        if m and r["modified"]:
            out.setdefault(m[1], []).append((r["team"], r["modified"]))
    return out


def state_rows(conn):
    """The rows both pages read, in the caller's read transaction: source health, agents, and each team's STATUS
    lines as read from the path structure.toml gives it today, and the teams with a linked ask report."""
    sources = [dict(r) for r in conn.execute("SELECT * FROM source_health ORDER BY source")]
    rows = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM agents ORDER BY name")}
    lines = {r["team"]: dict(r) for r in conn.execute("SELECT * FROM team_status")}
    asking = {r["team"] for r in conn.execute("SELECT team FROM reports WHERE linked AND has_ask")}
    return sources, rows, lines, asking


def team_lines(app, lines, key):
    """A team's three STATUS lines, all null when it has no STATUS or its path changed since the last read."""
    line = lines.get(key)
    if line and line.get("path") == app.structure.data.get("teams", {}).get(key, {}).get("status"):
        return line
    return dict.fromkeys(("working", "shipped", "waiting", "modified"))


def team_order(app, asking):
    """One team order for the home page and the office: teams that need Dylan (an open ask report, as in Needs you) first, chief among them, then the rest, each in structure.toml's order, with
    chief last among those."""
    keys = list(app.structure.data.get("teams", {}))
    need = [k for k in keys if k in asking]
    rest = [k for k in keys if k not in need]
    return need + [k for k in rest if k != "chief"] + [k for k in rest if k == "chief"]


def compose(app, sources, rows):
    src = next((s for s in sources if s["source"] == "hc"), {})
    as_of, snapshot = src.get("last_ok_at"), src.get("snapshot")
    declared = app.structure.declared()
    agents = []
    for name in [*declared, *(n for n in rows if n not in declared)]:
        a = rows.get(name) or dict.fromkeys(AGENT_COLUMNS) | {"name": name}
        if a["state_since_exact"] is not None:
            a["state_since_exact"] = bool(a["state_since_exact"])
        for k in ("rebuilt", "name_lost"):
            a[k] = bool(a[k])
        # Live means in the last successful snapshot; a failing herdr read leaves the last live set as it was.
        # Before the first successful snapshot nothing is known, so gone is null rather than true.
        a["gone"] = None if snapshot is None else a["snapshot"] != snapshot
        # Gone wins over any stored status; an unrecognized status is never shown as idle.
        a["state"] = None if a["gone"] is None else "gone" if a["gone"] else STATES.get(a["status"], "unknown")
        a["unseen"] = None if a["state"] is None else a["state"] == "idle" and a["status"] == "done"
        d = a["declared"] = declared.get(name)
        a["mascot_kind"] = d.get("kind") if d else "worker"
        a["runs_on"] = fleet.runs_on(a)
        if d:
            a |= {"team": d.get("team"), "parent": d.get("reports_to") or None, "placement_rule": "declared"}
        else:
            a |= place(a, app.structure, rows)
        agents.append(a)
    return {"as_of": as_of, "commit": app.commit, "deploy": app.deploy, "sources": sources,
            "teams": app.structure.team_names(), "agents": settle(agents)}


def shape(text):
    """The lengths of a screen's last 12 non-empty lines: all the office draws, never the text itself."""
    return [len(line.rstrip()) for line in (text or "").splitlines() if line.strip()][-12:]


def tail(text, n=30):
    """A screen from its n-th last non-empty line down, so blank padding above Claude's input box does not use up the n."""
    lines = (text or "").rstrip().split("\n")
    kept = [i for i, line in enumerate(lines) if line.strip()]
    return "\n".join(lines[kept[-n]:] if len(kept) > n else lines)


# The terminal view's read on demand, per pane: when the last read started, whether one is running, how many have
# finished, the last screen read (kept when a read fails) and the last error. One condition guards them all, so a
# pane never has two reads running. HELD is each agent's last screen by name, most recently used last, for its tab
# once it is not live.
READS, PANES, HELD, FRESH, HOLD_MAX = threading.Condition(), {}, OrderedDict(), 1.0, 256


def hold(name, pane, screen):
    """Keep an agent's last screen by name; past HOLD_MAX agents, the least recently used goes. Under READS."""
    HELD[name] = {"pane": pane, **screen}
    HELD.move_to_end(name)
    while len(HELD) > HOLD_MAX:
        HELD.popitem(last=False)


def prune(conn, live):
    """Drop the records of panes that left herdr's snapshot, never one with a read running (so a pane never has two),
    and an agent's held screen 24 hours after it was last live, as the office drops a Gone worker."""
    last = {r["name"]: r["last_seen"] for r in conn.execute("SELECT name, last_seen FROM agents")}
    cutoff = db.now(timedelta(hours=24))
    with READS:
        for pane in [p for p, r in PANES.items() if p not in live and not r["running"]]:
            del PANES[pane]
        for name in [n for n in HELD if (last.get(n) or "") < cutoff]:
            del HELD[name]


def answer(pane, r):
    """A pane record as the API gives it: the last screen held and the last error."""
    return {"pane": pane, **(r["screen"] or {"herdr_name": None, "text": None, "read_at": None}), "error": r["error"]}


def read_ansi(app, pane, name):
    """The pane's screen with its colours, read from herdr at most once a second; a failure keeps the last screen."""
    with READS:
        r = PANES.get(pane)
        # A new agent in the pane starts from nothing: it is never shown the screen of the one before.
        if r is None or (not r["running"] and r["screen"] and r["screen"]["herdr_name"] != name):
            r = PANES[pane] = {"started": None, "running": False, "done": 0, "screen": None, "error": None}
        if r["running"]:
            # A request that joins a read in flight never starts another: it takes the last result when there is one,
            # and otherwise waits for that read's own result, screen or error.
            if r["done"] == 0:
                READS.wait_for(lambda: r["done"] > 0)
            return answer(pane, r)
        # A read starts at most once a second, timed from the start of the last one.
        if r["started"] is not None and time.monotonic() - r["started"] < FRESH:
            return answer(pane, r)
        r["running"], r["started"] = True, time.monotonic()
    screen, error = None, None
    try:
        text = hc.read(app.hc, name)  # plain text: hc read gives no colours
        screen = {"herdr_name": name, "text": text, "read_at": db.now()}
    except RuntimeError as e:
        error = str(e)
    finally:
        with READS:
            r["screen"], r["error"] = screen or r["screen"], error
            if screen:
                hold(name, pane, screen)
            r["running"], r["done"] = False, r["done"] + 1
            READS.notify_all()
            out = answer(pane, r)
    return out


def screens(conn, app, query):
    """Every live pane's line lengths for the office; with ?pane=<id>, that one pane's full text for its popup; with
    ?pane=<id>&format=ansi, that pane read now with its colours, for the terminal view (live panes only); with
    ?agent=<name>&format=ansi, the last screen Atlas holds for that agent, for its view once it is not live."""
    pane, agent = query.get("pane", [None])[0], query.get("agent", [None])[0]
    if query.get("format") == ["ansi"] and (pane is not None or agent is not None):
        live = {r["pane"]: r["name"] for r in herdr.live_panes(conn)}
        prune(conn, live)
        if agent is not None:
            if agent == db.PRIVATE:
                return {"agent": agent, "pane": None, "herdr_name": None, "text": None, "read_at": None}
            # Never a pane read: a departed agent's old pane may hold another agent now.
            with READS:
                held = HELD.get(agent)
                if held:
                    HELD.move_to_end(agent)
            return {"agent": agent, **(held or {"pane": None, "herdr_name": None, "text": None, "read_at": None})}
        if pane not in live:
            raise NotFound
        if live[pane] == db.PRIVATE:
            return {"pane": pane, "herdr_name": db.PRIVATE, "text": None, "read_at": None}
        return read_ansi(app, pane, live[pane])
    if pane is not None:
        rows = conn.execute("SELECT pane, herdr_name, text, read_at FROM screens WHERE pane = ? AND herdr_name IS NOT ?", (pane, db.PRIVATE))
        return {"screens": [dict(r) for r in rows]}
    rows = conn.execute("SELECT pane, herdr_name, text, read_at FROM screens WHERE herdr_name IS NOT ? ORDER BY pane", (db.PRIVATE,))
    if query.get("text") == ["1"]:  # the agent grid: each held screen's last 30 non-empty lines in one read, with no read time, so an unchanged poll is a 304
        return {"screens": [{"herdr_name": r["herdr_name"], "text": tail(r["text"])} for r in rows]}
    return {"screens": [{"pane": r["pane"], "herdr_name": r["herdr_name"], "read_at": r["read_at"], "lines": shape(r["text"])}
                        for r in rows]}


def messages(conn, app, query):
    """The last 24 hours, newest first. While a transcript store is not ok the table is frozen: as_of is its last good write."""
    with db.read(conn):
        health = {r["source"]: dict(r) for r in conn.execute("SELECT * FROM source_health")}
        rows = conn.execute("SELECT * FROM messages WHERE time >= ? ORDER BY time DESC, key DESC",
                            (iso(time.time() - 24 * 3600),)).fetchall()
    m = health.get("messages", {})
    return {
        "as_of": m.get("last_ok_at"),
        "frozen": m.get("state") != "ok",
        "sources": [health[s] for s in ("claude", "codex", "registry", "messages") if s in health],
        "messages": [{
            "key": r["key"], "time": r["time"], "kind": r["kind"],
            "from": {"name": r["from_name"], "session": r["from_session"], "label": r["from_label"]},
            "to": {"name": r["to_name"], "session": r["to_session"], "label": r["to_label"]},
            "first_line": r["first_line"],
            "delivered": None if r["delivered"] is None else bool(r["delivered"]),
            "ambiguous": bool(r["ambiguous"]),
        } for r in rows],
    }


PICTURE_MAX = 5 << 20


def picture_file(app, team):
    """An open descriptor for the .png the team's current Last shipped line names, re-read from its STATUS, or None.
    The path is taken as written (a relative one from the STATUS file's folder) and must lie below one of the team's
    folders as spelt; it is opened by open_below from that folder, so no symlink is followed anywhere below it, and
    it must be a regular file under 5 MB. Any failure, a malformed path included, is no picture. The caller closes."""
    t = app.structure.data.get("teams", {}).get(team)
    if not isinstance(t, dict) or not isinstance(t.get("status"), str):
        return None
    try:
        source = expand(t["status"])
        with open(source, encoding="utf-8", errors="replace") as f:
            name = status.picture(status.three(f.read().splitlines())[0].get("shipped"))
        if name is None:
            return None
        path = os.path.normpath(os.path.join(os.path.dirname(source), expand(name)))
        for folder in t.get("folders", []):
            folder = os.path.normpath(expand(folder))
            relative = os.path.relpath(path, folder)
            if relative == os.curdir or relative == os.pardir or relative.startswith(os.pardir + os.sep):
                continue
            fd = open_below(folder, relative)
            if os.fstat(fd).st_size < PICTURE_MAX:
                return fd
            os.close(fd)
            return None
    except (OSError, ValueError):
        pass
    return None


IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}


def report_file(app, team, path):
    """An open descriptor for one image below the team's reports folder or one of its folders, or None. The path must
    be absolute, end in an image type and lie below a root as spelt; it is opened by open_below from that root, so a
    .. or a symlink anywhere below it is refused, and it must be under 5 MB. The caller closes."""
    t = app.structure.data.get("teams", {}).get(team)
    if not isinstance(t, dict) or not os.path.isabs(path) or Path(path).suffix.lower() not in IMAGES:
        return None
    found = reports.inside(path, reports.roots(t))
    try:
        fd = open_below(*found) if found else None
        if fd is not None and os.fstat(fd).st_size >= PICTURE_MAX:
            os.close(fd)
            return None
        return fd
    except OSError:
        return None


def shots(t, source, text):
    """Each backticked path in a label, in order and once: as written, resolved from the report's folder and ~, and
    whether it lies inside the team's roots. Web links are not paths."""
    out = []
    for written in dict.fromkeys(re.findall(r"`([^`\s]+)`", text or "")):
        if "://" not in written:
            path = os.path.normpath(os.path.join(os.path.dirname(source), expand(written)))
            out.append({"written": written, "path": path, "inside": reports.inside(path, reports.roots(t)) is not None})
    return out


def lede(labels):
    """The first sentence of What changed for a user, as written, or None."""
    text = labels.get("What changed for a user")
    return re.match(r"(.+?[.!?])(?=\s|$)|(.+)", text, re.S)[0].strip() if text else None


# A change, for /api/changes and the briefing's changes_since: a report in a reports folder changed since the For Dylan
# rule's start, not ask-only, not a brief (its title's first word is Brief, any case), and not written before the start
# by its own written time, or by its written date when only a date is known; a report with no written date keeps the
# mtime rule. Parameters :start_ns, :start and :day come from CHANGE_SINCE.
CHANGE = """in_reports AND mtime_ns >= :start_ns AND NOT (has_ask AND NOT has_change)
    AND NOT (lower(title) GLOB 'brief' OR lower(title) GLOB 'brief[^a-z]*')
    AND (written_at IS NULL OR written_at >= CASE WHEN written_at LIKE '%T%' THEN :start ELSE :day END)"""
CHANGE_SINCE = {"start_ns": reports.START_NS, "start": reports.START.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "day": reports.START.astimezone(status.ET).strftime("%Y-%m-%d")}


def changes(conn, app, query):
    """Every change (see CHANGE), newest first by written time and then modified time; ?team= for one team. A report
    with no block at all stays."""
    team = query.get("team", [None])[0]
    if team is not None and team not in view(app).structure.data.get("teams", {}):
        raise NotFound
    rows = conn.execute(f"""SELECT team, name, title, written_at, has_change, has_ask, change_labels FROM reports
                            WHERE {CHANGE} AND (:team IS NULL OR team = :team)
                            ORDER BY written_at DESC, mtime_ns DESC""", CHANGE_SINCE | {"team": team}).fetchall()
    return {"team": team, "since": reports.START.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "changes": [{"team": r["team"], "name": r["name"], "title": r["title"], "written_at": r["written_at"],
                         "has_change": bool(r["has_change"]), "has_ask": bool(r["has_ask"]), "lede": lede(json.loads(r["change_labels"] or "{}"))}
                        for r in rows]}


def report(conn, app, query):
    """One report by ?team= and ?name=, and the team's entry in structure.toml; NotFound when either is unknown."""
    team, name = (query.get(k, [None])[0] for k in ("team", "name"))
    row = conn.execute("SELECT * FROM reports WHERE team = ? AND name = ?", (team, name)).fetchone()
    t = app.structure.data.get("teams", {}).get(team)
    if row is None or not isinstance(t, dict):
        raise NotFound
    return row, t


def code_of(app, team, name):
    """The open change whose story is this report, {repo, n}, or None: the code review page it redirects to."""
    snap = app.review.snap
    for repo, r in (snap["repos"].items() if snap else ()):
        for c in r["changes"]:
            if c["story"] and (c["story"]["team"], c["story"]["name"]) == (team, name):
                return {"repo": repo, "n": review.cid(c), "number": c["n"], "branch": c["branch"]}
    return None


def change(conn, app, query):
    """One report's change block: labels as written, the Screens paths and the first pull request link."""
    row, t = report(conn, view(app), query)
    labels = json.loads(row["change_labels"] or "{}")
    return {"team": row["team"], "name": row["name"], "title": row["title"], "written_at": row["written_at"],
            "written_src": row["written_src"], "path": row["path"], "has_change": bool(row["has_change"]),
            "has_ask": bool(row["has_ask"]), "labels": labels, "shots": shots(t, row["path"], labels.get("Screens")),
            "pr": row["pr"], "review": code_of(app, row["team"], row["name"])}


def ask(conn, app, query):
    """One report's ask block, the Look at paths, the team's lead, and whether STATUS links it now, with that line."""
    app = view(app)
    row, t = report(conn, app, query)
    labels = json.loads(row["ask_labels"] or "{}")
    return {"team": row["team"], "name": row["name"], "title": row["title"], "path": row["path"],
            "has_change": bool(row["has_change"]), "has_ask": bool(row["has_ask"]), "labels": labels,
            "shots": shots(t, row["path"], labels.get("Look at")),
            "evidence_shots": [s for s in shots(t, row["path"], labels.get("Evidence")) if Path(s["path"]).suffix.lower() in IMAGES],
            "asked_at": row["asked_at"],
            "lead": app.structure.lead(row["team"]), "linked": bool(row["linked"]), "status_line": row["status_line"],
            "ask_id": f"{row['team']}/{row['name']}",
            "answer": answer_states(conn, app).get(f"{row['team']}/{row['name']}")}


# An answer sent from an ask page is not recorded until the team's decisions file says so: late after 30 minutes.
ANSWER_LATE = timedelta(minutes=30)
def answer_on_items(data):
    """The teams whose leads record an answer on the work item (work answer): every team of structure.toml with an
    items folder. Every team's decisions file is read as well."""
    return sorted(items.team_roots(data))


def ask_ident(ask_id):
    """The ask id as a whole token: not after a word character, slash, dot or hyphen, and ending at whitespace, ')',
    ',', ';', ':' or the end, or at a full stop that is itself followed by whitespace or the end. A full stop followed
    by anything else is a longer name (fixture..old, fixture.old), which never matches."""
    return r"(?<![\w/.-])" + re.escape(ask_id) + r"(?=[\s),;:]|$|\.(?:\s|$))"


def answer_letter(entry, ask_id):
    """The letter an entry gives, only in the form Dylan's own line carries: "(<ask id>): <LETTER>." or "<ask id>:
    <LETTER>.", with one capital letter, or the words Something else (a full stop or a colon after them, as his own
    words follow). No other wording gives a letter."""
    found = {m[1].rstrip(".:") for m in re.finditer(ask_ident(ask_id) + r"\)?: ([A-Z]\.|Something else[.:])", entry)}
    return found.pop() if len(found) == 1 else None  # two different letters for the ask give none


def answer_states(conn, app):
    """Each ask id with an answer sent, or recorded, to its state: sent, recorded or late. Sent is the newest send of
    kind answer (outcome sent) in sends; recorded is a decision entry, read from the file by StatusPoller, that names
    the whole ask id and was written after that send: in a later minute, or in the send's own minute when it gives
    the letter (Something else for other) the send picked, since an entry carries minutes only. Late is sent, 30
    minutes on, not recorded. With no send row (after a wipe), the newest entry that names the ask and gives its
    letter is recorded. Only the send is Atlas's own; recorded is read again from the file."""
    out, late, on_items = {}, db.now(ANSWER_LATE), answer_on_items(app.structure.data)
    shots = lambda s: [{"id": r["id"], "path": r["path"]} for r in conn.execute(  # noqa: E731
        "SELECT id, path FROM uploads WHERE send_id = ? ORDER BY at, rowid", (s["id"],))] if s else []
    sent = {r["key"]: r for r in conn.execute(
        """SELECT id, key, at, pick FROM sends WHERE kind = 'answer' AND outcome = 'sent'
           AND id IN (SELECT max(id) FROM sends WHERE kind = 'answer' AND outcome = 'sent' GROUP BY key)""")}
    for ask_id in sent.keys() | {f"{r['team']}/{r['name']}" for r in conn.execute("SELECT team, name FROM reports WHERE has_ask")}:
        team, s = ask_id.partition("/")[0], sent.get(ask_id)
        t = app.structure.data.get("teams", {}).get(team) or {}
        at = s["at"][:16] if s else ""
        found = None
        entries = [dict(r) for r in conn.execute("""SELECT value, written_at, NULL AS agent FROM observations
                   WHERE team = ? AND kind = 'decision' AND substr(written_at, 1, 16) >= ? ORDER BY written_at, seq""", (team, at))]
        if team in on_items:  # the team's item trails: only Dylan's own line, with the letter the send picked
            want = s and ("Something else" if s["pick"] == "other" else s["pick"])
            for r in conn.execute("SELECT text, at, agent FROM item_lines WHERE item LIKE ? AND instr(text, ?) > 0 ORDER BY item, seq",
                                  (team + ":%", ask_id)):
                letter = answer_letter(r["text"], ask_id)
                w = letter and (not want or letter == want) and items.line_time(r["at"] or "")
                if w and w[:16] >= at:
                    entries.append({"value": r["text"], "written_at": w, "agent": r["agent"]})
            entries.sort(key=lambda e: e["written_at"])  # stable: a decision entry first within one second
        for e in entries:
            letter = answer_letter(e["value"], ask_id)
            same = e["written_at"][:16] == at
            if s is None and letter:
                found = (e, letter)  # the newest wins
            elif s and re.search(ask_ident(ask_id), e["value"]) and (
                    not same or (letter and letter == ("Something else" if s["pick"] == "other" else s["pick"]))):
                found = (e, letter)
                break
        if found:
            out[ask_id] = {"state": "recorded", "sent_at": s and s["at"], "recorded_at": found[0]["written_at"],
                           "recorded_by": found[0]["agent"],
                           "letter": found[1], "pick": s and s["pick"], "shots": shots(s), "lead": app.structure.lead(team), "decisions": t.get("decisions")}
        elif s:
            out[ask_id] = {"state": "late" if s["at"] <= late else "sent", "sent_at": s["at"], "pick": s["pick"], "shots": shots(s),
                           "lead": app.structure.lead(team), "decisions": t.get("decisions")}
    return out


def briefing(conn, app, query, login):
    """The home page's data, read at one moment: each team's three lines and what Atlas saw change since this
    login's last look (without a login, since ?since=, from the browser), and what needs Dylan."""
    app = view(app)
    with db.read(conn):
        at = db.now()
        look = conn.execute("SELECT at FROM last_look WHERE login = ?", (login,)).fetchone() if login else None
        given = stamp(query.get("since", [None])[0]) if not login else None
        since = look["at"] if look else iso(given)[:19] + "Z" if given is not None else db.now(timedelta(hours=24))
        _, _, lines, asking = state_rows(conn)
        # What changed is the three lines and the decisions; STATUS log entries are working notes, never shown.
        decided = conn.execute("""SELECT id, team, kind, value, first_seen, written_at FROM observations
                                  WHERE kind = 'decision' AND first_seen > ?""", (since,)).fetchall()
        history = conn.execute("""SELECT id, team, kind, value, first_seen FROM observations
                                  WHERE kind IN ('working', 'shipped', 'waiting') ORDER BY id""").fetchall()
        # Asks: the reports STATUS links now that hold an ask block (spec criterion 4, amended 5:53 PM ET). Changes since the last look: /api/changes items written after it, by
        # their written time, or their modified time when only a date is known.
        asked = conn.execute("""SELECT team, name, ask_labels, asked_at FROM reports WHERE linked AND has_ask
                                ORDER BY asked_at DESC, name""").fetchall()
        answered = answer_states(conn, app)
        # Answered today: every ask with a send since midnight ET, linked or not, its question from the report row.
        midnight = datetime.now(ZoneInfo("America/New_York")).replace(hour=0, minute=0, second=0, microsecond=0)
        midnight = midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        today = []
        # When it was answered: the send, or with no send (after a wipe) the decision entry that recorded it.
        when = lambda a: a["sent_at"] or a.get("recorded_at") or ""
        for ask_id, a in sorted(answered.items(), key=lambda kv: when(kv[1]), reverse=True):
            if when(a) < midnight:
                continue
            team, _, name = ask_id.partition("/")
            row = conn.execute("SELECT ask_labels FROM reports WHERE team = ? AND name = ? AND has_ask", (team, name)).fetchone()
            today.append({"team": team, "name": name, "id": ask_id, "state": a["state"], "sent_at": a["sent_at"],
                          "recorded_at": a.get("recorded_at"), "lead": a["lead"],
                          "question": json.loads(row["ask_labels"] or "{}").get("Question") if row else None})
        since_changed = dict(conn.execute(f"""SELECT team, count(*) FROM reports WHERE {CHANGE}
                                AND CASE WHEN written_at LIKE '%T%' THEN written_at ELSE modified END > :since
                                GROUP BY team""", CHANGE_SINCE | {"since": since}).fetchall())
    # A decision reads as what was decided; one the feed cannot show plainly is not listed.
    new = [dict(r) | {"value": status.decision_news(r["value"])} for r in decided]
    new = [r for r in new if r["value"] is not None]
    # Each line at most once: its newest row, when that row changed inside the window (a baseline, as after a STATUS
    # path change, never did) and its value is not the one the line had at the last look (or the window's start),
    # cleared (null) included. Rows are in id order, so the last before the window is the value then.
    for team, kind in {(r["team"], r["kind"]) for r in history}:
        rs = [r for r in history if (r["team"], r["kind"]) == (team, kind)]
        if rs[-1]["first_seen"] is None or rs[-1]["first_seen"] <= since:
            continue
        start = next(r["id"] for r in rs if r["first_seen"] is not None and r["first_seen"] > since)
        before = [r for r in rs if r["id"] < start]
        if rs[-1]["value"] != (before[-1]["value"] if before else None):
            new.append(dict(rs[-1]) | {"written_at": None})
    # Newest first by the time the page shows: a decision's written time, a line's last change.
    new.sort(key=lambda r: (r["written_at"] or r["first_seen"], r["id"]), reverse=True)
    teams = []
    for key in team_order(app, asking):
        t, line = app.structure.data["teams"][key], team_lines(app, lines, key)  # one view: key is always there
        found = picture_file(app, key) if line["shipped"] else None
        if found is not None:
            os.close(found)
        teams.append({"key": key, "name": t.get("name") or key, "lead": app.structure.lead(key),
                      "working": line["working"], "shipped": line["shipped"], "waiting": line["waiting"],
                      "modified": line["modified"], "picture": found is not None, "pages": artifacts.listing(t),
                      "new": [{"kind": r["kind"], "value": r["value"], "first_seen": r["first_seen"],
                               "written_at": r["written_at"]} for r in new if r["team"] == key],
                      "asks": [{"name": r["name"], "question": json.loads(r["ask_labels"] or "{}").get("Question"),
                                "default": json.loads(r["ask_labels"] or "{}").get("Default"),
                                "asked_at": r["asked_at"], "answer": answered.get(f"{key}/{r['name']}")}
                               for r in asked if r["team"] == key],
                      "changes_since": since_changed.get(key, 0)})
    return {"login": login, "last_look": since, "as_of": at, "answered_today": today, "teams": teams}


def seen(conn, login):
    """This login's last look is now."""
    with conn:
        conn.execute("INSERT INTO last_look (login, at) VALUES (?, ?) ON CONFLICT (login) DO UPDATE SET at = excluded.at",
                     (login, db.now()))


# Login and X-Forwarded-For pairs already logged, in memory only; the device per client address for 60 s.
SEEN, SEEN_LOCK, DEVICES, WHO = set(), threading.Lock(), {}, threading.Lock()


def device(via):
    """The tailnet node at the request's address: its MagicDNS short name, for people to read, and its stable node ID,
    which ATLAS_TYPIST_DEVICES lists (a rename in the admin console changes the name, never the ID). (None, None) on any
    failure. The address is the last X-Forwarded-For entry, the one tailscale serve appends; earlier entries come from
    the client."""
    try:
        address = str(ipaddress.ip_address((via or "").split(",")[-1].strip()))
    except ValueError:
        return None, None
    with WHO:  # one whois per address at a time, so joiners take its answer
        now = time.monotonic()
        for a in [a for a, (at, _) in DEVICES.items() if now - at >= 60]:
            del DEVICES[a]
        if address not in DEVICES:
            try:
                p = subprocess.run(["tailscale", "whois", "--json", address], capture_output=True, text=True, timeout=2)
                node = json.loads(p.stdout)["Node"] if p.returncode == 0 else {}
                found = (node["Name"].split(".")[0] or None, node["StableID"] or None) if node else (None, None)
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError):
                found = (None, None)
            DEVICES[address] = (now, found)
        return DEVICES[address][1]


def socket_uid(ends):
    """The uid that owns the client end of this TCP connection, or None: the /proc/net/tcp row whose local address is
    the request's peer and whose remote address is this server's end, in state ESTABLISHED (01). Any other state counts
    as no owner: the kernel shows uid 0 for a socket its owner has closed (FIN_WAIT2, TIME_WAIT), so a local process
    that sent Dylan's headers and closed at once would otherwise pass as tailscaled. Addresses there are hex, the IPv4
    address in little-endian byte order and the port big-endian. Atlas binds 127.0.0.1 only, so /proc/net/tcp6 is
    never needed."""
    peer, local = ends

    def address(field):
        ip, port = field.split(":")
        return socket.inet_ntoa(struct.pack("<I", int(ip, 16))), int(port, 16)
    try:
        with open("/proc/net/tcp", encoding="ascii") as f:
            for line in list(f)[1:]:
                cols = line.split()
                if address(cols[1]) == peer and address(cols[2]) == local:
                    return int(cols[7]) if cols[3] == "01" else None
    except (OSError, ValueError, IndexError, struct.error):
        pass
    return None


def identity(app, headers, ends):
    """The Tailscale login and name tailscale serve attached to this request, None when absent, and its client
    address. The three headers count only when the client end of the connection (ends: the peer's and this server's
    address and port) is owned by tailscaled, uid 0 (ATLAS_SERVE_UID on a check instance only): any other process on
    this box could send Dylan's headers to 127.0.0.1. Otherwise, or when the owner cannot be found, the request is
    anonymous. Logs each new pair of login and forwarding addresses once, with the device, quoted as data."""
    login, name, via = (headers.get(h) for h in ("Tailscale-User-Login", "Tailscale-User-Name", "X-Forwarded-For"))
    if (login, name, via) != (None, None, None) and socket_uid(ends) != app.serve_uid:
        login = name = via = None
    with SEEN_LOCK:
        new = (login, via) not in SEEN
        SEEN.add((login, via))
    if new:
        node = device(via)[0]
        print(f"identity: login={'none' if login is None else json.dumps(login)} "
              f"via={'none' if via is None else json.dumps(via)} device={'none' if node is None else json.dumps(node)}",
              file=sys.stderr, flush=True)
    return login, name, via


def whoami(app, headers, ends):
    """Who sent this request; typist only for the configured login, exactly, from a device on the list, by its stable
    node ID. Only a typist, on a same-origin request, gets send_token, which POST /api/send requires."""
    login, name, via = identity(app, headers, ends)
    node, node_id = device(via)
    typist = bool(app.typist) and login == app.typist and node_id in app.devices
    # The token goes only to the typist's own Atlas page: a same-origin request (a sandboxed mock's is not).
    return {"login": login, "name": name, "device": node, "device_id": node_id, "typist": typist} | (
        {"send_token": SEND_TOKEN} if typist and headers.get("Sec-Fetch-Site") == "same-origin" else {})


def viewer(app, headers, ends):
    """The login whose last look this request reads and sets: its Tailscale login, only from one of Dylan's own
    devices (ATLAS_TYPIST_DEVICES, resolved as /api/whoami does). Any other request, such as a render from this box
    or a server VM that carries his login, is anonymous: None."""
    login, _, via = identity(app, headers, ends)
    return login if login and device(via)[1] in app.devices else None


# POST /api/send: a line or a key into a live agent's terminal, for the typist only, audited in the kept table sends.
SEND_TOKEN = secrets.token_urlsafe(32)  # per process; /api/whoami gives it to the typist only
SEND_MAX = 8 << 10  # bytes of body
SEND_TEXT_MAX = 4000  # characters in an answer, a review line, and the reply box on an ask page
# A message from an agent's page box (kind line) may hold line breaks and run to 100,000 bytes of UTF-8. Its JSON body
# is at most twice that, since a line break or a quote costs two bytes there and every other control is refused, and room for the keys.
SEND_LINE_BYTES = 100_000
SEND_LINE_BODY_MAX = 2 * SEND_LINE_BYTES + 512
# The keys a typist may send, by the page's name, to hc's own key names.
# Never in a line or an agent name: controls (Cc), format characters such as bidi overrides and zero-width spaces (Cf),
# Unicode line and paragraph separators (Zl, Zp), and unpaired surrogates (Cs), which JSON allows but argv cannot carry.
REFUSED = {"Cc": "control", "Cf": "control", "Zl": "control", "Zp": "control", "Cs": "surrogate"}
# A char is one printable ASCII character (0x20 to 0x7E) sent as a key. `hc keys` takes a key by name, not a character:
# each of these was checked on a throwaway terminal, where it typed that character. A capital, and a sign that needs
# Shift (! " # $ % & ( ) * + : < > ? @ ^ _ { | } ~), is rejected by hc keys or arrives without its Shift, so it is refused
# here (bad-body, audited "char") and never sent as a different character.
CHAR_NAMES = {" ": "space"}
HC_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789 ',-./;=[\\]`")
SEND_KEYS = {"Enter": "enter", "Esc": "esc", "Tab": "tab", "Shift+Tab": "shift+tab", "Ctrl+C": "ctrl+c", "Up": "up",
             "Down": "down", "Left": "left", "Right": "right", "Backspace": "backspace", "y": "y", "n": "n"}


class Refused(Exception):
    """A send refused: the HTTP code, the reason word the page shows, and the reason audited when it is finer."""
    def __init__(self, code, reason, audited=None):
        super().__init__(reason)
        self.code, self.reason, self.audited = code, reason, audited or reason


def send_target(conn, app, body, fields):
    """The checked request's hc command, (command, agent name, text or key) for hc.send; fields, the audit fields, are filled in as they are known.
    The agent must be a named, live hc agent in Atlas's current read (hc ok), never a pane id or a bare shell; a line is 1 to 4,000 characters with no
    character of a REFUSED category, newline included (a message of kind line may hold newlines, and runs to 100,000 bytes of UTF-8);
    a key is one of SEND_KEYS. A line to a blocked agent is refused (hc would
    refuse it without --force); a key or a char is not, since keys answer its question. An answer is a line with an ask id: the id of a
    linked report with an ask block, sent to that team's lead only, audited as kind answer with the id in key, the pick
    (a letter or other) and the length. A char is one printable ASCII character, audited
    as kind char with length 1. Neither the text nor a char is ever in the audit fields."""
    if not isinstance(body, dict) or body.get("kind") not in ("line", "key", "char", "answer", "review"):
        raise Refused(400, "bad-body")
    box = body["kind"] == "line"  # a message from the agent page's box; a review is composed into a line below but keeps its limits
    if body["kind"] == "review":
        fields["kind"] = "review"
        body = review_line(conn, app, body, fields) | {"kind": "line"}
    lined = body["kind"] in ("line", "answer")
    if set(body) != {"agent", "kind", "text" if lined else "key"} | ({"ask", "pick"} if body["kind"] == "answer" else set()):
        raise Refused(400, "bad-body")
    fields.setdefault("kind", body["kind"])
    if lined:
        text = body["text"]
        if not isinstance(text, str):
            raise Refused(400, "bad-body")
        fields["length"] = len(text)
        if not text or not box and len(text) > SEND_TEXT_MAX:
            raise Refused(400, "bad-body", "length")
        if bad := next((REFUSED[k] for c in text if (k := unicodedata.category(c)) in REFUSED and not (box and c == "\n")), None):
            raise Refused(400, "bad-body", bad)
        if box and len(text.encode("utf-8")) > SEND_LINE_BYTES:  # after the scan: a lone surrogate cannot be encoded
            raise Refused(400, "too-long")
        if body["kind"] == "answer":
            ask, pick = body["ask"], body["pick"]
            team, _, name = ask.partition("/") if isinstance(ask, str) else ("", "", "")
            # A linked report with an ask block only; an id or a pick with a control character is never audited.
            if (not isinstance(pick, str) or not re.fullmatch(r"[A-Z]|other", pick) or any(unicodedata.category(c) in REFUSED for c in ask)
                    or not conn.execute("SELECT 1 FROM reports WHERE team = ? AND name = ? AND has_ask AND linked",
                                        (team, name)).fetchone()):
                raise Refused(400, "bad-body", "ask")
            fields |= {"key": ask, "pick": pick}
    elif body["kind"] == "char":
        if not isinstance(body["key"], str) or len(body["key"]) != 1 or body["key"] not in HC_CHARS:
            raise Refused(400, "bad-body", "char")
        fields["length"] = 1  # never the character
    elif not isinstance(body["key"], str) or body["key"] not in SEND_KEYS:
        raise Refused(400, "bad-body", "key")
    else:
        fields["key"] = body["key"]
    if body["agent"] == db.PRIVATE:  # the page hides it; the server refuses it too
        raise Refused(400, "gone")
    row = conn.execute(
        """SELECT a.name, a.pane, a.status FROM agents a
           JOIN source_health h ON h.source = 'hc' AND a.snapshot = h.snapshot AND h.state = 'ok'
           WHERE a.name = ? AND a.pane IS NOT NULL AND a.name != a.pane AND a.runtime IS NOT NULL""",
        (body["agent"] if isinstance(body["agent"], str)
         and not any(unicodedata.category(c) in REFUSED for c in body["agent"]) else None,)).fetchone()
    if row is None:
        raise Refused(400, "gone")
    fields |= {"agent": row["name"], "pane": row["pane"]}
    if body["kind"] == "answer" and app.structure.lead(team) != row["name"]:
        raise Refused(400, "bad-body", "lead")
    if lined and row["status"] == "blocked":
        raise Refused(400, "blocked")
    if lined:
        return "send", row["name"], body["text"]
    if body["kind"] == "char":
        return "keys", row["name"], CHAR_NAMES.get(body["key"], body["key"])
    return "keys", row["name"], SEND_KEYS[body["key"]]


def review_line(conn, app, body, fields):
    """A review comment body {kind, repo, n, comments: [{path, line, comment}]} as the one line of the fixed form, to the
    change's owner: the same body send_target checks as a line. Paths must be files of the change's diff. Audited as kind
    review with the repo and PR as the key and the line's length."""
    if set(body) - {"to"} != {"kind", "repo", "n", "comments"} or not isinstance(body["repo"], str) or not isinstance(body["comments"], list) \
            or not 1 <= len(body["comments"]) <= 100 or not isinstance(body["n"], (str, int)):
        raise Refused(400, "bad-body")
    try:
        c = app.review.find(body["repo"], body["n"])
    except NotFound:
        raise Refused(400, "unknown-change") from None
    to = body.get("to")
    if "to" in body and ("teams" not in c or not isinstance(to, str) or to not in c["teams"]):
        raise Refused(400, "bad-body", "to")  # a single-team change takes no to; a shared one only a declaring team
    if "teams" in c and "to" not in body:
        raise Refused(400, "bad-body", "to")
    files = {f["path"] for f in review.stats(c).get("files", [])}
    for x in body["comments"]:
        if not (isinstance(x, dict) and set(x) == {"path", "line", "comment"} and x["path"] in files and type(x["line"]) is int
                and x["line"] >= 1 and isinstance(x["comment"], str) and x["comment"].strip()):
            raise Refused(400, "bad-body", "comment")
    fields["key"] = f"{c['repo']}#{review.cid(c)}"
    return {"agent": review.owner(app, conn, c, to)["name"], "text": review.compose(c, body["comments"])}


# POST /api/upload: a screenshot for an answer, in ~/teams/atlas/data/uploads/<date>/<id>.<png|jpg> beside the database.
UPLOAD_MAX = PICTURE_MAX - 1  # bytes: the most the ask page shows
UPLOAD_PER_ANSWER = 5


def image_type(data):
    """png or jpg when the first bytes say so and the last close the file (a PNG ends with its IEND chunk, a JPEG with
    FFD9, trailing zeros allowed), else None. The name and the type header are never looked at."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(b"IEND\xaeB`\x82"):
        return "png"
    if data.startswith(b"\xff\xd8\xff") and data.rstrip(b"\0").endswith(b"\xff\xd9"):
        return "jpg"
    return None


def uploads_root(app):
    return os.path.join(os.path.dirname(os.path.abspath(app.db)), "uploads")


def store_upload(root, data, ext):
    """(id, full path) of data stored as root/<YYYY-MM-DD>/<random id>.<ext>. Every folder is made and entered by
    descriptor with O_NOFOLLOW, so no symlink is followed; the bytes go to a temporary name (O_EXCL), are checked by size,
    and get their name by a hard link, which never replaces a file; the temporary name is then removed. On any failure
    nothing is left. OSError on failure."""
    day = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    fds = [open_root(os.path.dirname(root))]
    try:
        for part in (os.path.basename(root), day):
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=fds[-1])
            fds.append(open_dir(part, fds[-1]))
        tmp = f".{secrets.token_hex(8)}.part"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fds[-1])
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
                if os.fstat(f.fileno()).st_size != len(data):
                    raise OSError("short write")
            for _ in range(5):
                uid = secrets.token_hex(8)
                try:
                    os.link(tmp, f"{uid}.{ext}", src_dir_fd=fds[-1], dst_dir_fd=fds[-1], follow_symlinks=False)
                except FileExistsError:
                    continue
                return uid, os.path.join(root, day, f"{uid}.{ext}")
            raise OSError("no free name")
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=fds[-1])
    finally:
        for d in fds:
            os.close(d)


def audit_upload(conn, who, outcome, fields):
    """One uploads row. Never the image."""
    with conn:
        conn.execute(
            """INSERT INTO uploads (id, at, login, device, device_id, ask, path, size, sha256, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (fields.get("id") or secrets.token_hex(8), db.now(), who["login"], who["device"], who["device_id"], fields.get("ask"),
             fields.get("path"), fields.get("size"), fields.get("sha256"), outcome))


def keep_uploads(conn, send_id, ask, text):
    """Every stored upload of this ask whose path the sent answer line names belongs to that send (the newest), so a
    replacement answer keeps the pictures it names again."""
    try:
        with conn:
            for r in conn.execute("SELECT id, path FROM uploads WHERE ask = ? AND outcome = 'stored'", (ask,)).fetchall():
                if r["path"] in text:
                    conn.execute("UPDATE uploads SET send_id = ? WHERE id = ?", (send_id, r["id"]))
    except sqlite3.Error:
        pass  # the answer is sent; its pictures just do not show on the ask page


def audit_send(conn, who, outcome, fields, row_id=None, client_key=None):
    """Write one sends row, or set the outcome of row_id; returns the row's id. Never the text. client_key is the page's
    key for the send: sqlite3.IntegrityError when another row has it."""
    with conn:
        if row_id is not None:
            conn.execute("UPDATE sends SET outcome = ? WHERE id = ?", (outcome, row_id))
            return row_id
        return conn.execute(
            """INSERT INTO sends (at, login, device, device_id, agent, pane, kind, key, pick, length, outcome, client_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (db.now(), who["login"], who["device"], who["device_id"], fields.get("agent"), fields.get("pane"),
             fields.get("kind"), fields.get("key"), fields.get("pick"), fields.get("length"), outcome, client_key)).lastrowid


CLIENT_KEY = re.compile(r"[0-9A-Za-z-]{8,64}")


def take_client_key(body):
    """The page's key for this send, removed from the body (None when it sent none); Refused when it is not a short word."""
    if not isinstance(body, dict) or "client_key" not in body:
        return None
    key = body.pop("client_key")
    if not isinstance(key, str) or not CLIENT_KEY.fullmatch(key):
        raise Refused(400, "bad-body", "client-key")
    return key


def repeat_reply(conn, key):
    """The kept row that has this key, as the already-sent answer's body, or None."""
    row = conn.execute("SELECT outcome, at FROM sends WHERE client_key = ?", (key,)).fetchone()
    return None if row is None else {"reason": "already-sent", "outcome": row["outcome"], "at": row["at"]}


# History answers, by file, size and cursor: an unchanged transcript costs a stat, not a read.
HISTORY = OrderedDict()
HISTORY_LOCK = threading.Lock()


def history(conn, app, query):
    """?agent=<name>[&before=<cursor>]: the agent's current transcript (its session as herdr last saw it) as History
    items, oldest first, with the cursor for Earlier. No transcript Atlas reads: {"items": [], "none": true}."""
    name = query.get("agent", [""])[0]
    if name == db.PRIVATE:
        return {"agent": name, "items": [], "none": True}
    before = query.get("before", [""])[0]
    before = int(before) if before.isdigit() else None
    row = conn.execute("SELECT runtime, session_id FROM agents WHERE name = ?", (name,)).fetchone()
    found = transcripts.transcript_path(row["runtime"], row["session_id"], time.monotonic()) if row else None
    if found is None:
        return {"agent": name, "items": [], "none": True}
    names = {r["name"] for r in conn.execute("SELECT name FROM agents")}

    def who(origin, text):
        """"from <name>" for a message that names its sender (a SendMessage, or a herdr prompt that opens with the
        sender's name, as the teams write them); "typed in the terminal" otherwise."""
        if origin is not None:
            sock = origin.get("from") if isinstance(origin.get("from"), str) else ""
            hit = conn.execute(
                """SELECT a.name FROM session_names s JOIN agents a ON a.session_id = s.session_id
                   WHERE (s.socket = ? AND s.socket != '') OR s.registry_name = ? ORDER BY s.socket = ? DESC, s.last_seen DESC
                   LIMIT 1""", (sock.removeprefix("uds:"), origin.get("name"), sock.removeprefix("uds:"))).fetchone()
            sender = hit["name"] if hit else origin.get("name")
            return f"from {sender}" if isinstance(sender, str) and sender else "from another session"
        m = re.match(r"\[(?:WORKER )?([A-Za-z0-9_.:-]+)\]|([a-z][a-z0-9_-]*): ", text or "")
        sender = m and (m.group(1) or m.group(2))
        return f"from {sender}" if sender in names else "typed in the terminal"

    try:
        # Opened by walking descriptors down from the store's root, so a symlink anywhere is refused (PR 38 review B).
        fd = open_below(*found)
        try:
            st = os.fstat(fd)
            key = (*found, st.st_ino, st.st_size, st.st_mtime_ns, before)
            with HISTORY_LOCK:
                hit = HISTORY.get(key)
                if hit:
                    HISTORY.move_to_end(key)
            if not hit:
                items, earlier = transcripts.history(fd, row["runtime"], before, who)
                hit = {"items": items, "earlier": earlier}
                with HISTORY_LOCK:
                    HISTORY[key] = hit
                    while len(HISTORY) > 32:
                        HISTORY.popitem(last=False)
        finally:
            os.close(fd)
    except OSError:
        return {"agent": name, "items": [], "none": True}
    return {"agent": name, "session": row["session_id"], "none": False, **hit, "items": mine(conn, name, hit["items"])}


MINE_WITHIN = timedelta(minutes=10)


def mine(conn, name, items):
    """The items with his own marked: each sent line, answer or review to this agent (a sends row of outcome sent, with
    its length) claims the first "in" item after it, within 10 minutes, whose whole text has that length and that reads
    "typed in the terminal" (no named sender). The cached
    items are not changed; a marked one is a copy with who "Dylan, via Atlas" and mine true. No text is stored."""
    # Only an item with no named sender can be his: an agent's message, "from <name>", never is.
    ins = [i for i in items if i["kind"] == "in" and "n" in i and i["who"] == "typed in the terminal"]
    if not ins:
        return items
    when = lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")) if isinstance(t, str) else None
    try:
        first = min(t for i in ins if (t := when(i["at"])))
    except (ValueError, TypeError):
        return items
    rows = conn.execute("SELECT at, length FROM sends WHERE agent = ? AND kind IN ('line', 'answer', 'review') AND outcome = 'sent' "
                        "AND at >= ? ORDER BY at, id", (name, (first - MINE_WITHIN).strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchall()
    taken = set()
    for r in rows:
        sent = when(r["at"])
        for k, i in enumerate(ins):
            try:
                t = when(i["at"])
            except ValueError:
                continue
            # A second's slack for the two clocks being read at different moments.
            if k not in taken and i["n"] == r["length"] and t and sent - timedelta(seconds=1) <= t <= sent + MINE_WITHIN:
                taken.add(k)
                break
    hit = {id(ins[k]) for k in taken}
    return [{**i, "who": "Dylan, via Atlas", "mine": True} if id(i) in hit else i for i in items]


def pr_args(query):
    repo, n = (query.get(k, [None])[0] for k in ("repo", "n"))
    if not repo or not n:
        raise NotFound
    return repo, n


def review_queue(conn, app, login):
    app = view(app)
    with db.read(conn):
        asking = state_rows(conn)[3]
    return review.queue(app, conn, login, team_order(app, asking))


def item(conn, app, query):
    row = items.one(conn, query.get("id", [""])[0], app.review.snap, view(app).structure.data.get("teams", {}))
    if row is None:
        raise NotFound
    return row


ROUTES = {"/api/state": state, "/api/screens": screens, "/api/history": history, "/api/messages": messages,
          "/api/changes": changes, "/api/change": change, "/api/ask": ask,
          "/api/pr": lambda conn, app, query: review.pr(app, conn, *pr_args(query)),
          "/api/pr-review": lambda conn, app, query: review.review_doc(app, *pr_args(query)),
          "/api/pr-diff": lambda conn, app, query: review.pr_diff(app, *pr_args(query), query),
          "/api/lineage": lambda conn, app, query: lineage.get(app, *pr_args(query)),
          "/api/usage": lambda conn, app, query: usage.get() | {"agents": fleet.agents(state(conn, app, {"all": ["1"]})["agents"]),
                                                                 "worktrees": fleet.worktrees(app)},
          "/api/items": lambda conn, app, query: items.listing(conn, app.review.snap, view(app).structure.data.get("teams", {})),
          "/api/item": item}

STATIC = Path(__file__).resolve().parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8", ".svg": "image/svg+xml",
         ".json": "application/json; charset=utf-8", ".png": "image/png"}


def static_files():
    """Request path to file for the page and every file under atlas/static, built once at start.
    A request is only ever looked up in this table, never joined to a path."""
    files = {"/static/" + f.relative_to(STATIC).as_posix(): f for f in sorted(STATIC.rglob("*")) if f.is_file()}
    return {"/": STATIC / "home.html", "/teams": STATIC / "home.html", "/office": STATIC / "teams.html", "/agents": STATIC / "agents.html", "/agent/": STATIC / "agent.html",
            "/changes": STATIC / "report.html", "/ask/": STATIC / "report.html", "/change/": STATIC / "report.html",
            "/review": STATIC / "review.html", "/usage": STATIC / "usage.html", "/pr/": STATIC / "review.html",
            "/raincli/": STATIC / "raincli.html", "/work": STATIC / "work.html", "/work/": STATIC / "work.html", "/standup": STATIC / "standup.html",
            "/favicon-32.png": STATIC / "favicon-32.png", "/apple-touch-icon.png": STATIC / "apple-touch-icon.png", **files}


# Top-level stamps that move on a read with nothing new, each with the header that carries its real value: as_of is the read time of
# every route that has one; last_look is the briefing's "24 hours before now" for a viewer with no login; read_at is a screen's read time.
STAMPS = {"as_of": "X-Atlas-As-Of", "last_look": "X-Atlas-Last-Look", "read_at": "X-Atlas-Read-At"}


def blanked(path, answer):
    """The answer with the stamps that move on every read set to None, for its ETag. Each is named by its path in the answer.
    The top-level STAMPS are blanked. /api/state: an agent's snapshot and last_seen, a source's snapshot and, while the source is
    ok, its last_ok_at (home.js reads it only when herdr is not ok). /api/review: each repo's fetched. No page reads the others."""
    if not isinstance(answer, dict):
        return answer
    out = dict(answer)
    out.update({k: None for k in STAMPS if k in out})
    if path == "/api/state":
        out["agents"] = [{**a, "snapshot": None, "last_seen": None} for a in out["agents"]]
        out["sources"] = [{**s, "snapshot": None, **({"last_ok_at": None} if s["state"] == "ok" else {})} for s in out["sources"]]
    if path == "/api/review" and "repos" in out:
        out["repos"] = {k: {**r, "fetched": None} for k, r in out["repos"].items()}
    return out


class Handler(BaseHTTPRequestHandler):
    app = None
    files = {}
    timeout = 10  # seconds any one socket read or write may stall; not a deadline on the whole request

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/api/send-status":
            return self.get_send_status(parse_qs(query))
        if path == "/api/whoami":
            return self.send(json.dumps(whoami(self.app, self.headers, self.ends()), ensure_ascii=False).encode(),
                             "application/json; charset=utf-8")
        identity(self.app, self.headers, self.ends())
        if path == "/api/shipped-picture":
            found = picture_file(self.app, parse_qs(query).get("team", [""])[0])
            try:
                body = read_fd(found, PICTURE_MAX) if found is not None else None
            except OSError:
                body = None
            if body is None or len(body) >= PICTURE_MAX:  # none, unreadable, or grown since the check
                return self.reply(404, "text/plain; charset=utf-8", b"Not found.\n")
            return self.send(body, "image/png")
        if path == "/api/report-file":
            q = parse_qs(query)
            found = report_file(view(self.app), q.get("team", [""])[0], q.get("path", [""])[0])
            try:
                body = read_fd(found, PICTURE_MAX) if found is not None else None
            except OSError:
                body = None
            if body is None or len(body) >= PICTURE_MAX:
                return self.reply(404, "text/plain; charset=utf-8", b"Not found.\n")
            return self.send(body, IMAGES[Path(q["path"][0]).suffix.lower()])
        if path == "/api/upload":
            return self.get_upload(parse_qs(query).get("id", [""])[0])
        if path.startswith("/mocks/"):  # the mocks moved to Atlas's own artifacts folder
            return self.reply(301, "text/plain; charset=utf-8", b"", None, {"Location": "/a/atlas/" + path.removeprefix("/mocks/")})
        if path.startswith("/a/"):
            team, _, relative = unquote(path.removeprefix("/a/")).partition("/")
            t = view(self.app).structure.data.get("teams", {}).get(team)
            root = artifacts.folder(t) if t else None
            try:
                found = artifacts.read(root, relative, TYPES) if root else None
            except (OSError, ValueError):  # unreadable, removed mid-republish, or a symlink met: all simply not found
                found = None
            if found is None:
                return self.reply(404, "text/plain; charset=utf-8", b"Not found.\n")
            # A page runs sandboxed in an opaque origin: its script cannot read Atlas's answers (the send token) and its
            # requests are not same-origin, so it can never send as the typist who opens it. It loads only its team's files.
            headers = {"Content-Security-Policy": artifacts.csp(self.headers.get("Host"), team)}
            if relative.endswith(".woff2"):  # a sandboxed page's font request is cross-origin and needs this to load
                headers["Access-Control-Allow-Origin"] = "*"
            return self.send(found[0], found[1], headers)
        # Every /agent/<name> is the one page agent.html, and every /ask/<team>/<name> and /change/<team>/<name> the
        # one page report.html; each reads what to show from its own address.
        prefix = next((p for p in ("/agent/", "/ask/", "/change/", "/pr/", "/work/", "/raincli/") if path.startswith(p)), path)
        file = self.files.get(prefix)
        if file is not None:
            page = file.read_bytes()
            if file.name == "agent.html":  # the page knows the private agent from its first byte, before any state
                page = page.replace(b'<body class="tv-page">', f'<body class="tv-page" data-private="{db.PRIVATE}">'.encode())
            return self.send(page, TYPES.get(file.suffix, "application/octet-stream"))
        if path == "/api/raincli-thread":
            return self.raincli_thread(parse_qs(query).get("id", [""])[0])
        route = ROUTES.get(path)
        if path == "/api/briefing":
            login = viewer(self.app, self.headers, self.ends())
            route = lambda conn, app, query: briefing(conn, app, query, login)  # noqa: E731
        if path == "/api/review":
            login = viewer(self.app, self.headers, self.ends())
            route = lambda conn, app, query: ({"waiting_for_review": review_queue(conn, app, login)["waiting_for_review"]}
                                              if query.get("count") == ["1"] else review_queue(conn, app, login))  # noqa: E731
        if route is None:
            return self.reply(404, "text/plain; charset=utf-8", b"Not found.\n")
        conn = db.connect(self.app.db)
        try:
            answer = route(conn, self.app, parse_qs(query))
        except NotFound:
            return self.reply(404, "application/json; charset=utf-8", b'{"error": "not found"}\n')
        finally:
            conn.close()
        body = json.dumps(answer, ensure_ascii=False).encode()
        # The ETag is that of the answer with its read-time stamps blanked, so a poll whose answer has not changed gets a 304; a 304 carries the
        # answer's own stamps for the page to put back.
        extra = {h: answer[k] for k, h in STAMPS.items() if isinstance(answer, dict) and isinstance(answer.get(k), str)}
        self.send(body, "application/json; charset=utf-8", extra, json.dumps(blanked(path, answer), ensure_ascii=False).encode())

    def get_upload(self, uid):
        """A stored upload, by its id only: the id must be the uploads row of a stored file, whose path is opened below the
        uploads folder (no symlink followed) and read up to PICTURE_MAX. Anything else is 404."""
        conn, body = db.connect(self.app.db), None
        try:
            row = conn.execute("SELECT path FROM uploads WHERE id = ? AND outcome = 'stored'", (uid,)).fetchone() if re.fullmatch(r"[0-9a-f]{16}", uid) else None
            if row:
                root = uploads_root(self.app)
                try:
                    body = read_fd(open_below(root, os.path.relpath(row["path"], root)), PICTURE_MAX)
                except OSError:
                    body = None
        finally:
            conn.close()
        if body is None or len(body) >= PICTURE_MAX:
            return self.reply(404, "text/plain; charset=utf-8", b"Not found.\n")
        self.send(body, "image/png" if body.startswith(b"\x89PNG") else "image/jpeg", {"X-Content-Type-Options": "nosniff"})

    def raincli_thread(self, cid):
        """One thread, read live from RainCLI for this request. The answer is never stored or cached."""
        conn = db.connect(self.app.db)
        try:
            data = raincli.page(conn, self.app.raincli, cid)
        finally:
            conn.close()
        if data is None:
            return self.reply(404, "application/json; charset=utf-8", b'{"error": "not found"}\n')
        self.reply(200, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode(), None,
                   {"Cache-Control": "no-store"})

    def ends(self):
        """This connection's two ends: the peer's address and port, and this server's."""
        return self.client_address[:2], self.connection.getsockname()[:2]

    def do_POST(self):
        """POST /api/send (post_send), and POST /api/seen, only from Dylan's own device (viewer) on a same-origin
        request (or one that names no site), whose body is never read. The connection closes after the answer, so an
        unread body cannot hold the thread."""
        self.close_connection = True
        if self.path.partition("?")[0] == "/api/send":
            return self.post_send()
        if self.path.partition("?")[0] == "/api/review-mark":
            return self.post_mark()
        if self.path.partition("?")[0] == "/api/upload":
            return self.post_upload()
        login = viewer(self.app, self.headers, self.ends())
        if self.path.partition("?")[0] != "/api/seen":
            return self.reply(404, "text/plain; charset=utf-8", b"Not found.\n")
        site = self.headers.get("Sec-Fetch-Site")
        if login is None or site is not None and site != "same-origin":
            return self.reply(403, "text/plain; charset=utf-8", b"Forbidden.\n")
        conn = db.connect(self.app.db)
        try:
            seen(conn, login)
        finally:
            conn.close()
        self.reply(204, None, b"")

    def typist_gates(self, who):
        """The first four checks of every typist-only request, in order, or Refused."""
        if socket_uid(self.ends()) != self.app.serve_uid:
            raise Refused(403, "not-serve")
        if not who["typist"]:
            raise Refused(403, "not-typist")
        if self.headers.get("Sec-Fetch-Site") != "same-origin":
            raise Refused(403, "cross-site")
        if not hmac.compare_digest(self.headers.get("X-Atlas-Send-Token", "").encode(), SEND_TOKEN.encode()):
            raise Refused(403, "token")

    def get_send_status(self, query):
        """GET /api/send-status?key=<client key>: what became of a send the page lost the reply to, behind the gates of
        POST /api/send in their order. {"outcome": sent, sending, failed or not-found, "at": the row's time or null}: no
        text, agent or row id."""
        who = whoami(self.app, self.headers, self.ends())
        conn = db.connect(self.app.db)
        try:
            try:
                self.typist_gates(who)
                key = query.get("key", [""])[0]
                if not CLIENT_KEY.fullmatch(key):
                    raise Refused(400, "bad-body", "client-key")
            except Refused as e:
                return self.reply(e.code, "application/json; charset=utf-8", json.dumps({"reason": e.reason}).encode(), None, {"Cache-Control": "no-store"})
            row = conn.execute("SELECT outcome, at FROM sends WHERE client_key = ?", (key,)).fetchone()
            body = {"outcome": row["outcome"], "at": row["at"]} if row else {"outcome": "not-found", "at": None}
            self.reply(200, "application/json; charset=utf-8", json.dumps(body).encode(), None, {"Cache-Control": "no-store"})
        finally:
            conn.close()

    def gate(self, who, limit=SEND_MAX, raw=False):
        """The checks every typist-only POST passes, in order (see post_send), then the JSON body of at most limit bytes
        (raw: the body's bytes, its length of up to 7 digits), or Refused."""
        self.typist_gates(who)
        # Exactly one Content-Length of 1 to 6 ASCII digits (7 for raw), and no Transfer-Encoding at all.
        sizes = self.headers.get_all("Content-Length") or []
        if "Transfer-Encoding" in self.headers or len(sizes) != 1 or not re.fullmatch(r"[0-9]{1,%d}" % (7 if raw else 6), sizes[0]):
            raise Refused(400, "bad-body", "framing")
        size = sizes[0]
        if int(size) > limit:
            raise Refused(400, "bad-body", "too-large")  # never read: the connection closes
        try:
            body = self.rfile.read(int(size))
            if raw:
                if len(body) != int(size):  # the client left before its last byte
                    raise Refused(400, "bad-body", "short")
                return body
            body = json.loads(body)
        except (ValueError, OSError, RecursionError):  # RecursionError: JSON nested too deep
            raise Refused(400, "bad-body") from None
        return body

    def post_send(self):
        """A line or a key into an agent's terminal, checked in this order: the connection comes from tailscaled
        (not-serve), the typist (not-typist), Sec-Fetch-Site exactly same-origin, a missing header refused (cross-site),
        the send token (token); only then the body, at most 200 KB of JSON (see send_target; 8 KB for the other POSTs). Every request is one sends
        row, written before hc runs and given its outcome after: sent, failed or refused:<reason>. 204, 403 or 400
        with {"reason": ...}, or 502 with no detail when hc fails. A body may carry client_key, a word the page made for
        this press: its row keeps it (a unique index decides two requests at once), and a request whose key is already on
        a row runs nothing and gets 409 {"reason": "already-sent", "outcome", "at"} of that row, audited refused:repeat."""
        who = whoami(self.app, self.headers, self.ends())
        conn, fields = db.connect(self.app.db), {}
        try:
            try:
                body = self.gate(who, SEND_LINE_BODY_MAX)
                key = take_client_key(body)
                if key and (again := repeat_reply(conn, key)):  # before the target: a repeat of a send that went never fails on the agent
                    raise Refused(409, "already-sent", "repeat")
                argv = send_target(conn, self.app, body, fields)
            except Refused as e:
                audit_send(conn, who, "refused:" + e.audited, fields)
                return self.reply(e.code, "application/json; charset=utf-8", json.dumps(again if e.code == 409 else {"reason": e.reason}).encode())
            try:
                row_id = audit_send(conn, who, "sending", fields, client_key=key)
            except sqlite3.IntegrityError:  # another request took the key between the check and here
                audit_send(conn, who, "refused:repeat", fields)
                return self.reply(409, "application/json; charset=utf-8", json.dumps(repeat_reply(conn, key)).encode())
            try:
                hc.send(self.app.hc, *argv)
            except Exception:  # any failure, not only hc's own: a row never stays sending
                audit_send(conn, who, "failed", fields, row_id)
                return self.reply(502, None, b"")
            audit_send(conn, who, "sent", fields, row_id)
            if fields.get("kind") == "answer":
                keep_uploads(conn, row_id, fields["key"], argv[-1])
            self.reply(204, None, b"")
        finally:
            conn.close()

    def post_upload(self):
        """One screenshot for an ask's answer: the image's bytes, with the ask id in X-Atlas-Ask, for the typist through
        the gates of POST /api/send (the body at most UPLOAD_MAX bytes, checked before a byte is read). PNG or JPEG by
        its first bytes, and whole (its last bytes close it), up to UPLOAD_PER_ANSWER unsent per ask. Every request is
        one uploads row, refusals included. 200 with {"id", "path"}, 403 or 400 with {"reason"}, or 500 when the file
        could not be stored (then none is left)."""
        who = whoami(self.app, self.headers, self.ends())
        conn, fields = db.connect(self.app.db), {}
        try:
            try:
                data = self.gate(who, UPLOAD_MAX, raw=True)
                fields |= {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                ask = self.headers.get("X-Atlas-Ask", "")
                team, _, name = ask.partition("/")
                if (not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", ask) or not conn.execute(
                        "SELECT 1 FROM reports WHERE team = ? AND name = ? AND has_ask AND linked", (team, name)).fetchone()):
                    raise Refused(400, "bad-body", "ask")
                fields["ask"] = ask
                ext = image_type(data)
                if ext is None:
                    raise Refused(400, "bad-image")
                if conn.execute("SELECT count(*) FROM uploads WHERE ask = ? AND outcome = 'stored' AND send_id IS NULL AND at > ?",
                                (ask, db.now(timedelta(hours=1)))).fetchone()[0] >= UPLOAD_PER_ANSWER:
                    raise Refused(400, "too-many")
            except Refused as e:
                audit_upload(conn, who, "refused:" + e.audited, fields)
                return self.reply(e.code, "application/json; charset=utf-8", json.dumps({"reason": e.reason}).encode())
            try:
                uid, path = store_upload(uploads_root(self.app), data, ext)
            except OSError:
                audit_upload(conn, who, "failed", fields)
                return self.reply(500, "application/json; charset=utf-8", b'{"reason": "store"}')
            try:
                audit_upload(conn, who, "stored", fields | {"id": uid, "path": path})
            except Exception:  # no row, no file
                with contextlib.suppress(OSError):
                    os.unlink(path)
                raise
            self.reply(200, "application/json; charset=utf-8", json.dumps({"id": uid, "path": path}).encode())
        finally:
            conn.close()

    def post_mark(self):
        """Mark a change reviewed, or take the mark off: body {repo, n, head, reviewed}, for the typist through the
        gates of POST /api/send, written to the kept table review_marks under the typist's login. 204, 403 or 400."""
        who = whoami(self.app, self.headers, self.ends())
        conn = db.connect(self.app.db)
        try:
            try:
                body = self.gate(who)
                if not (isinstance(body, dict) and set(body) == {"repo", "n", "head", "reviewed"} and isinstance(body["repo"], str)
                        and isinstance(body["n"], (str, int)) and isinstance(body["head"], str) and isinstance(body["reviewed"], bool)):
                    raise Refused(400, "bad-body")
                review.set_mark(conn, self.app, who["login"], body["repo"], body["n"], body["head"], body["reviewed"])
            except NotFound:
                return self.reply(404, "application/json; charset=utf-8", b'{"error": "not found"}\n')
            except Refused as e:
                return self.reply(e.code, "application/json; charset=utf-8", json.dumps({"reason": e.reason}).encode())
            self.reply(204, None, b"")
        finally:
            conn.close()

    def send(self, body, content_type, extra=None, same=None):
        """same: the bytes whose change makes a new answer, when the rest (a read time) moves every read."""
        etag = '"' + hashlib.sha256(body if same is None else same).hexdigest()[:32] + '"'
        if self.headers.get("If-None-Match") == etag:
            return self.reply(304, None, b"", etag, extra)  # extra: the stamp headers on a poll
        # gzip when the request allows it; the ETag stays that of the plain body, and Vary tells caches the body depends on the request.
        extra = {**(extra or {}), "Vary": "Accept-Encoding"}
        if len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", "") and content_type.startswith(("text/", "application/json", "application/javascript", "image/svg")):
            body, extra["Content-Encoding"] = gzip.compress(body, 1), "gzip"
        self.reply(200, content_type, body, etag, extra)

    def reply(self, code, content_type, body, etag=None, extra=None):
        self.send_response(code)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if content_type:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
        if etag:
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, code="-", size="-"):
        self.code = code  # kept for the slow-request line; nothing else is logged per request

    def handle_one_request(self):
        """One line to the log for a request slower than 1 s, one that ended in a 5xx, and one whose client hung up or failed
        mid-response: method, path without its query, status, milliseconds. No query value, body or header."""
        self.code, t0, failed = "-", time.monotonic(), None
        try:
            super().handle_one_request()
        except Exception as e:  # BrokenPipeError and ConnectionResetError are the client leaving: logged here, not as a traceback
            failed = type(e).__name__
            self.close_connection = True
            if not isinstance(e, (BrokenPipeError, ConnectionResetError)):
                raise
        finally:
            ms = round((time.monotonic() - t0) * 1000)
            if getattr(self, "command", None) and (failed or ms > 1000 or str(self.code).startswith("5")):
                print(f"slow-request {self.command} {self.path.partition('?')[0]} {self.code} {ms}ms{' ' + failed if failed else ''}", flush=True)


def serve(app, port):
    Handler.app, Handler.files = app, static_files()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.serve_forever()
