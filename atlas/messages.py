"""Messages between agents, recomputed from all transcript evidence in the 25-hour window on each poll and written
as a diff, and placement rule 1. The rows are a function of the evidence and the stored observations alone."""
import os
import re
import time
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import timedelta

from . import db, reports
from .structure import expand
from .files import open_below, read_fd
from .transcripts import Evidence, iso, read_stores, stamp

WINDOW = 25 * 3600
PAIR, PROMPT, LAUNCH = 120, 900, 60  # seconds: block after send, receive after command, first event after launch
COLUMNS = ("key", "time", "kind", "from_session", "from_name", "from_label", "from_raw",
           "to_session", "to_name", "to_label", "to_raw", "first_line", "delivered", "ambiguous")


def norm(text):
    return " ".join(text.split())


def stem(name):
    """A registered name without its two-character suffix: lead-df is lead."""
    m = re.fullmatch(r"(.+)-[0-9a-z]{2}", name)
    return m.group(1) if m else name


def first_line(text):
    return next((line.strip() for line in text.splitlines() if line.strip()), "")[:140]


def real(path):
    return os.path.realpath(expand(path)) if path else None


def registered(to):
    """A SendMessage `to` as a registered name: without ListAgents' bracketed ref; a socket path is not a name."""
    to = re.sub(r"\s*\[[0-9a-f]+\]$", "", (to or "").strip())
    return to if to and not to.startswith("uds:") else None


class Names:
    """Session to herdr name, and the observations that hold for a given time. Read once per poll."""

    def __init__(self, conn, ev, structure):
        self.ev = ev
        self.agent = {r["session_id"]: r["name"] for r in conn.execute(
            "SELECT name, session_id FROM agents WHERE session_id IS NOT NULL AND name_lost IS NOT 1 ORDER BY last_seen")}
        self.herdr, self.registry = defaultdict(list), defaultdict(list)
        for r in conn.execute("SELECT * FROM session_names ORDER BY last_seen"):
            if r["herdr_name"]:
                self.herdr[r["session_id"]].append((r["herdr_name"], r["pane"], stamp(r["first_seen"]), stamp(r["last_seen"])))
            if r["registry_name"]:  # by registered name and by messaging socket, as a SendMessage `to` gives them
                for key in (r["registry_name"], "uds:" + r["socket"] if r["socket"] else None):
                    self.registry[key].append((r["session_id"], stamp(r["name_since"] or r["first_seen"]), stamp(r["last_seen"])))
        declared = structure.declared()
        folders = Counter(real(a.get("folder")) for a in declared.values() if a.get("folder"))
        self.declared = {real(a["folder"]): n for n, a in declared.items() if a.get("folder") and folders[real(a["folder"])] == 1}
        self.folders = set(folders) - {None}  # every declared folder, shared ones included
        self.teams = [real(f) for t in structure.data.get("teams", {}).values() for f in t.get("folders", [])]
        # A launch names the one session that starts 0 to 60 s after it in the pane's cwd, which is known only
        # from a herdr observation of that agent in that pane.
        pane_cwd = {(r["name"], r["pane"]): real(r["cwd"]) for r in conn.execute(
            "SELECT name, pane, cwd FROM agents WHERE pane IS NOT NULL AND cwd IS NOT NULL")}
        named = defaultdict(set)
        for launch in ev.launches.values():
            if launch.pane is None:
                continue
            cwd = pane_cwd.get((launch.name, launch.pane))
            found = [s for s, x in ev.sessions.items()
                     if cwd and s != launch.sid and 0 <= x.first - launch.t <= LAUNCH and real(x.cwd) == cwd]
            if len(found) == 1:
                named[found[0]].add((launch.name, launch.pane, launch.t))
        self.launched = {s: v.pop() for s, v in named.items() if len(v) == 1}  # session -> (name, pane, time)
        self.cache = {}

    def name(self, sid):
        """herdr's session value, a herdr observation, a declared folder, a launch command, a worker folder, or None."""
        if sid not in self.cache:
            self.cache[sid] = self.agent.get(sid) or (self.herdr[sid][-1][0] if self.herdr.get(sid) else None) \
                or self.from_folders(sid) or (self.launched.get(sid) or (None,))[0] or self.worker(sid)
        return self.cache[sid]

    def from_folders(self, sid, cwd=None):
        """The declared agent A for this session where it worked in cwd (its latest cwd when not given, as display
        naming uses), or None. A only when the session was launched exactly in A's folder (its first event's cwd), cwd
        is A's folder or inside it by whole path components (Forge's engineering-lead works in /notes), and no other
        declared folder is a longer match for cwd. A session that has moved into another declared agent's folder is no
        longer A by its folder alone."""
        s = self.ev.sessions.get(sid)
        cwd = cwd or (s.cwd if s else None)
        if not s or not s.launch_cwd or not cwd:
            return None
        launch, cwd = real(s.launch_cwd), real(cwd)
        held = [f for f in self.folders if os.path.commonpath([cwd, f]) == f]
        return self.declared.get(launch) if held and max(held, key=len) == launch else None

    def worker(self, sid):
        s = self.ev.sessions.get(sid)
        cwd = real(s.cwd) if s and s.cwd else None
        for team in self.teams if cwd else ():
            parts = os.path.relpath(cwd, team).split(os.sep)
            if len(parts) >= 2 and parts[0] == "workers" and parts[1] not in ("", ".", ".."):
                return parts[1]
        return None

    def label(self, sid):
        s = self.ev.sessions.get(sid)
        return self.name(sid) or f"{s.runtime if s else 'session'} {sid[:8]}"

    def end(self, sid):
        return (sid, self.name(sid), self.label(sid))

    def target(self, target, sid, t, cwd=None):
        """Whether a herdr prompt's target was the receiving session at time t, where the receive's own cwd was cwd
        (the session's latest cwd when not given). A herdr observation of that session whose interval covers t
        decides, for or against: no fallback overrides it. Without one: the declared agent from_folders() gives for
        that cwd (a declaration, so it holds for all time and survives a wipe), unless herdr has seen this session as
        another agent; else the launch command that named it (from its time on); else its worker folder.
        Display naming (name()) is not used: a name observed at another time says nothing about t."""
        seen = self.herdr.get(sid, ())
        covering = [(n, pane) for n, pane, first, last in seen if first <= t <= last]
        if covering:
            return any(target in names for names in covering)
        if target == self.from_folders(sid, cwd) and all(n == target for n, *_ in seen):
            return True
        launch = self.launched.get(sid)
        if launch and launch[2] <= t and target in launch[:2]:
            return True
        return target == self.worker(sid)


def build(ev, names, now):
    """Every row the evidence gives, by key, and the counts for the last 24 hours."""
    rows, taught = {}, defaultdict(list)
    blocks = sorted(ev.blocks.values(), key=lambda b: (b.t, b.key))
    sends = sorted(ev.sends.values(), key=lambda s: (s.t, s.key))
    by_text = defaultdict(list)
    for s in sends:
        result = ev.results.get(s.id)
        s.ok, s.recipient = (result.ok, result.to or registered(s.to)) if result else (None, registered(s.to))
        by_text[norm(s.text)].append(s)
    blocks_by_text = defaultdict(list)
    for b in blocks:
        blocks_by_text[norm(b.text)].append(b)

    def owned(name, t):
        """Sessions the registry says a name or socket belonged to at time t."""
        return {sid for sid, since, until in names.registry.get(name, ()) if since <= t <= until} if name else set()

    def known(name, t):
        """Sessions a registered name is known to have been at time t, for pairing: registry rows, and earlier unique
        pairs (which have no end, so they never name an end on their own)."""
        return (owned(name, t) | {sid for sid, since in taught.get(name, ()) if since <= t}) if name else set()

    def through_registry(t, *keys):
        """The one session the registry gives the named names and sockets at time t, for an end no pairing named, and
        whether it stays unresolved because more than one remains or they disagree. Only ownership valid at t counts,
        never a learned pair. A key with no registry row at t does not constrain."""
        found = [k for k in (owned(key, t) for key in keys) if k]
        sids = set.intersection(*found) if found else set()
        two = len(sids) > 1 or (len(found) > 1 and not sids)  # before the one session is taken out of the set
        return (sids.pop() if len(sids) == 1 else None), two

    def fits(s, b):
        if not 0 <= b.t - s.t <= PAIR or s.sid == b.sid:
            return False
        if s.recipient:
            if not b.cwd or stem(s.recipient) != os.path.basename(b.cwd.rstrip("/")):
                return False
            k = known(s.recipient, s.t)
            if k and b.sid not in k:
                return False
        # A send to a socket carries no registered name for the stem rule; it fits only the session the socket is
        # known to have been (the page is silent on sockets; this is the conservative reading).
        elif not (s.to or "").startswith("uds:") or b.sid not in known(s.to, s.t):
            return False
        return all(not f or s.sid in f for f in (known(b.frm, b.t), known(b.sock, b.t)))

    # SendMessage: one to one in time order. A block or send with more than one remaining candidate pairs with none.
    paired, used, ambiguous, blocked, receive_only = {}, set(), set(), set(), []
    for b in blocks:
        fitting = [s for s in by_text[norm(b.text)] if fits(s, b)]
        if not fitting:
            receive_only.append(b)  # no send on this box passes the pairing conditions
            continue
        cands = [s for s in fitting if s.key not in paired]
        if not cands:
            continue
        others = [x for x in blocks_by_text[norm(b.text)] if x.key not in used and fits(cands[0], x)]
        if len(cands) == 1 and others == [b] and cands[0].key not in ambiguous and b.key not in blocked:
            s = cands[0]
            paired[s.key] = b
            used.add(b.key)
            taught[s.recipient or s.to].append((b.sid, s.t))
            for name in (b.frm, b.sock):
                if name:
                    taught[name].append((s.sid, b.t))
            continue
        blocked.add(b.key)
        for s in cands:
            ambiguous.add(s.key)
            blocked.update(x.key for x in blocks_by_text[norm(b.text)] if x.key not in used and fits(s, x))
    for s in sends:
        b = paired.get(s.key)
        # An unpaired send names its recipient by registered name or socket: the session the registry gives at its time.
        sid, two = (b.sid, False) if b else through_registry(s.t, s.recipient or s.to)
        to = names.end(sid) if sid else (None, None, s.recipient or s.to)
        rows[s.key] = (s.key, iso(s.t), "send", *names.end(s.sid), b.frm if b else None, *to, s.to,
                       first_line(s.text), None if s.ok is None else int(s.ok), int((s.key in ambiguous or two) and not b))
    for b in receive_only:
        # A block with no send on this box names its sender's registered name and socket: resolved the same way.
        sid, two = through_registry(b.t, b.frm, b.sock)
        sender = names.end(sid) if sid else (None, None, b.frm or "sender not known")
        rows[b.key] = (b.key, iso(b.t), "receive_only", *sender, b.frm,
                       *names.end(b.sid), None, first_line(b.text), 1, int(two))

    # herdr prompts: one to one in time order; more than one remaining candidate, or none, is "sender not known".
    occurrences = sorted(ev.occurrences.values(), key=lambda o: (o.t, o.key))
    times, consumed, senders = [o.t for o in occurrences], set(), {}
    for r in sorted(ev.prompts.values(), key=lambda r: (r.t, r.key)):
        text = norm(r.text)
        cands = [o for o in occurrences[bisect_left(times, r.t - PROMPT):bisect_right(times, r.t)]
                 if o.key not in consumed and o.sid != r.sid
                 and (text == o.text if o.literal else text.startswith(o.text)) and names.target(o.target, r.sid, r.t, r.cwd)]
        if len(cands) == 1:
            consumed.add(cands[0].key)
            senders[r.key] = cands[0].sid
        sender = names.end(senders[r.key]) if r.key in senders else (None, None, "sender not known")
        rows[r.key] = (r.key, iso(r.t), "prompt", *sender, None, *names.end(r.sid), None, first_line(r.text), 1, 0)

    day = now - 24 * 3600
    counts = {
        "sends": sum(s.t >= day for s in sends),
        "received_blocks": sum(b.t >= day for b in blocks),
        "pairs": sum(s.t >= day for s in sends if s.key in paired),
        "ambiguous_sends": sum(s.t >= day for s in sends if s.key in ambiguous and s.key not in paired),
        "receive_only": sum(b.t >= day for b in receive_only),
        "prompts_named": sum(r.t >= day for r in ev.prompts.values() if r.key in senders),
        "prompts_not_known": sum(r.t >= day for r in ev.prompts.values() if r.key not in senders),
        "runtime_excluded": sum(t >= day for t in ev.excluded.values()),
    }
    return rows, {b.key: s for s, b in paired.items()}, counts


def first_reach(ev, rows, pairs, sid):
    """(key, sender's herdr name) of the first message that reached a session, or None when that first message has
    no named sender: an ambiguous or receive-only block, a prompt whose sender is not known, a sender with no name."""
    receipts = [(x.t, x.key) for d in (ev.blocks, ev.prompts) for x in d.values() if x.sid == sid]
    if not receipts:
        return None
    key = min(receipts)[1]
    row = rows.get(pairs.get(key) if key.startswith("recv:") else key)
    return (row[0], row[4]) if row and row[4] else None


BRIEF_MAX = 64 << 10  # bytes of a brief read for its From line


def home(structure, rows, name):
    """A message sender's workspace, which the message rule requires the agent to share: its own agent row's (rows: by
    herdr name), else its declared team's. None when it is on neither."""
    d = structure.declared().get(name)
    return (rows.get(name) or {}).get("workspace") or (
        structure.data.get("teams", {}).get(d.get("team"), {}).get("workspace") if d else None)


def team_roots(t):
    """The one set of roots for the brief rule and pairing: a team's folders and its reports path, ~ expanded and
    normalised as spelt, and the second list, the `briefs` folders beside its reports and items folders (any *.md in
    one is a brief). Those are roots as well."""
    spelt = list(t.get("folders", [])) + [t.get("reports")]
    roots = [os.path.normpath(expand(r)) for r in spelt if isinstance(r, str) and r]
    briefs = []
    for key in ("reports", "items"):
        if isinstance(t.get(key), str) and t[key]:
            b = os.path.join(os.path.dirname(os.path.normpath(expand(t[key]))), "briefs")
            if b not in briefs:
                briefs.append(b)
    return roots + [b for b in briefs if b not in roots], briefs


def is_brief(path, rel, briefs):
    """A *.md under <root>/records/briefs/ or a *-brief.md anywhere below a root, or any *.md below a briefs folder (the
    path decides that one: such a folder may sit inside a team folder that is the root a file is opened from)."""
    return rel.endswith(".md") and (rel.startswith("records/briefs/") or rel.endswith("-brief.md")
                                    or any(path.startswith(b + "/") for b in briefs))


def briefed_by(ev, sid, roots, briefs=()):
    """(prompt key, briefing agent) when the session's first prompt names a brief inside one of roots (team_roots: the agent's team
    folders, reports path and briefs folders, ~ expanded, as spelt): a *.md under <root>/records/briefs/, a *-brief.md anywhere below, or any *.md in a briefs folder. The agent is
    the name in backticks right after "From " on the brief's first line starting "From ". The brief is opened by
    open_below from its root, so no symlink or .. is followed, and only its first 64 KB are read. None otherwise."""
    prompts = sorted((p.t, p.key, p.text) for p in ev.prompts.values() if p.sid == sid)
    if not prompts:
        return None
    _, key, text = prompts[0]
    for token in re.findall(r"[^\s`'\"()<>\[\]]+\.md", text):
        path = os.path.normpath(expand(token))
        root = next((r for r in roots if os.path.isabs(path) and path.startswith(r.rstrip("/") + "/")), None)
        rel = path[len(root.rstrip("/")) + 1:] if root else ""
        if not root or not is_brief(path, rel, briefs):
            continue
        try:
            head = read_fd(open_below(root, rel), BRIEF_MAX).decode("utf-8", errors="replace")
        except OSError:
            continue
        line = next((l for l in head.splitlines() if l.startswith("From ")), "")
        m = re.match(r"From `([^`]+)`", line)  # the sender only: never a later name on the line, such as the recipient
        return (key, m[1]) if m else None
    return None


FROM = re.compile(r"From `([^`]+)` to `([^`]+)`")
PAIRS_TTL = 60  # seconds a team's walk of briefs and reports is kept: a new file places an agent within a minute


class Pairing:
    """Rule "pairing": per team, who briefed each agent, and whom each agent reported to, by the first "From " line of
    every brief and report in team_roots. The walk is kept PAIRS_TTL seconds and each file's line until its mtime or size
    changes. Inside the roots only, by open_below: no symlink or .. is followed, and the first 64 KB are read."""

    def __init__(self):
        self.lines, self.walks = {}, {}  # path -> (mtime_ns, size, line); team -> (time, briefs, reports)

    def index(self, team, t, now):
        hit = self.walks.get(team)
        if hit and now - hit[0] < PAIRS_TTL:
            return hit[1], hit[2]
        roots, briefs = team_roots(t)
        reports_root = os.path.normpath(expand(t["reports"])) if isinstance(t.get("reports"), str) and t["reports"] else None
        by_brief, by_report, walked = {}, {}, []
        for root in roots:
            if any(root.startswith(w.rstrip("/") + "/") for w in walked):
                continue  # inside a root already walked
            try:
                found, _ = reports.walk(root)
            except OSError:
                continue
            walked.append(root)
            for rel, st in found:
                path = os.path.join(root, rel)
                kind = "brief" if is_brief(path, rel, briefs) else (
                    "report" if reports_root and path.startswith(reports_root + "/") else None)
                if not kind or st.st_size > BRIEF_MAX * 16:
                    continue
                cached = self.lines.get(path)
                if cached and cached[:2] == (st.st_mtime_ns, st.st_size):
                    line = cached[2]
                else:
                    try:
                        head = read_fd(open_below(root, rel), BRIEF_MAX).decode("utf-8", errors="replace")
                    except OSError:
                        continue
                    line = next((l for l in head.splitlines() if l.startswith("From ")), "")
                    self.lines[path] = (st.st_mtime_ns, st.st_size, line)
                m = FROM.match(line)
                if m:
                    a, b = m[1], m[2]
                    if kind == "brief":
                        by_brief.setdefault(b, []).append((st.st_mtime, a))
                    else:
                        by_report.setdefault(a, []).append((st.st_mtime, b))
        self.walks[team] = (now, by_brief, by_report)
        return by_brief, by_report

    def parent(self, team, t, name, now):
        """The agent a brief names as the writer for name, else the one its newest report went to, newest file first.
        None when neither exists or it names the agent itself."""
        by_brief, by_report = self.index(team, t, now)
        for found in (by_brief.get(name), by_report.get(name)):
            if found and (a := max(found)[1]) != name:
                return a
        return None


class TranscriptPoller:
    interval = 5

    def __init__(self, conn, structure, rebuilt):
        self.conn, self.structure = conn, structure
        self.rebuild = rebuilt  # the database is new: rebuild the Gone set once, after the first complete replay
        self.ev = Evidence(time.time() - WINDOW)
        self.counts = None
        self.pairing = Pairing()
        with conn:  # until the replay of every store has finished, messages keep their last good rows
            db.set_health(conn, "messages", db.now(), "failing", "frozen: replaying the stores after a start")

    def poll(self):
        now, at = time.time(), db.now()
        self.ev.prune(now - WINDOW)
        health = read_stores(self.ev, now - WINDOW)
        with self.conn:
            for source, (state, error, failed) in health.items():
                db.set_health(self.conn, source, at, state, error, failed)
            # Each Claude agent's now line, kept up to date even while the message list is frozen.
            self.conn.executemany(
                "UPDATE agents SET now = ?, now_at = ? WHERE session_id = ? AND name IS NOT ? AND (now IS NOT ? OR now_at IS NOT ?)",
                [(s.now, iso(s.now_at), sid, db.PRIVATE, s.now, iso(s.now_at)) for sid, s in self.ev.sessions.items() if s.now])
            self.conn.executemany(
                "UPDATE agents SET account = ?, model = ? WHERE session_id = ? AND (account IS NOT ? OR model IS NOT ?)",
                [(s.account, s.model, sid, s.account, s.model) for sid, s in self.ev.sessions.items() if s.model])
            frozen = [f"{s} {h[0]}" for s, h in health.items() if h[0] != "ok"]
            if frozen:  # a partial read never deletes, rewrites or re-pairs a row
                db.set_health(self.conn, "messages", at, "failing", "frozen: " + ", ".join(frozen))
                return
        names = Names(self.conn, self.ev, self.structure)
        rows, pairs, counts = build(self.ev, names, now)
        i, j, k = COLUMNS.index("from_name"), COLUMNS.index("to_name"), COLUMNS.index("first_line")
        mine = {db.PRIVATE, *db.private_sessions(self.conn)}  # by name, or by a session that never had one
        for key, r in rows.items():  # an outside person's words never reach the table
            if mine & {r[i], r[j], r[COLUMNS.index("from_session")], r[COLUMNS.index("to_session")]}:
                rows[key] = r[:k] + (None,) + r[k + 1:]
        placed = self.placements(names, rows, pairs, now)
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            old = {r[0]: tuple(r) for r in self.conn.execute(f"SELECT {', '.join(COLUMNS)} FROM messages")}
            self.conn.executemany("DELETE FROM messages WHERE key = ?", [(k,) for k in old if k not in rows])
            self.conn.executemany(
                f"INSERT OR REPLACE INTO messages ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                [r for k, r in rows.items() if old.get(k) != r])
            self.conn.executemany(
                """UPDATE agents SET placement_rule = ?, placed_by = ?, placed_by_key = ?, placed_session = ?
                   WHERE name = ?""", placed)
            if self.rebuild and self.rebuild_gone(names, rows, now):
                self.rebuild = False
            db.set_health(self.conn, "messages", at)
        if counts != self.counts:
            self.counts = counts
            print("messages, last 24 h: " + " ".join(f"{k}={v}" for k, v in counts.items()), flush=True)

    def placements(self, names, rows, pairs, now):
        """For each unlisted agent, live or Gone for less than 24 hours, the first rule that gives a parent: "launch"
        (the agent whose session ran `herdr agent start <name>`), "message" (the named sender of the first message that
        reached its session), "brief" (the agent that wrote the brief its first prompt names), then "pairing" (a brief
        or report in its team's roots that names it). web's place() checks the parent is on the chart and settle() that
        it is in the agent's team with no cycle. A session whose first event is inside the window is read from the
        evidence on every poll; an older one is replayed once, back to its first event. Launch and pairing are worked
        out again on every poll, so a later start line or report can place an agent that no rule has placed."""
        declared = self.structure.declared()
        agents = {r["name"]: {"workspace": r["workspace"], "cwd": r["cwd"]} for r in self.conn.execute("SELECT name, workspace, cwd FROM agents")}
        snap = self.conn.execute("SELECT snapshot FROM source_health WHERE source = 'hc'").fetchone()
        snap = snap[0] if snap else None
        out = []
        for a in self.conn.execute("SELECT * FROM agents WHERE last_seen >= ?", (db.now(timedelta(hours=24)),)):
            if a["name"] in declared:
                continue
            sid = a["session_id"] or next((s for s in self.ev.sessions if names.name(s) == a["name"]), None)
            s = self.ev.sessions.get(sid)
            team = self.structure.team_by_folder(a["cwd"]) or self.structure.team_by_workspace(a["workspace"])
            t = self.structure.data.get("teams", {}).get(team) or {}
            if parent := self.launcher(names, a, s, agents, declared, team):
                out.append(("launch", parent, None, sid, a["name"]))
                continue
            if (a["placement_rule"] == "launch" and snap is not None and a["snapshot"] == snap
                    and not any(l.name == a["name"] for l in self.ev.launches.values())):
                continue  # live, and its start has left the evidence window: the stored result stands, as message and brief do
            kept, result = self.first_rules(names, rows, pairs, now, a, sid, s, t, agents)
            if kept or (result and result[0]):
                if result:
                    out.append(result)
                continue
            if t and (parent := self.pairing.parent(team, t, a["name"], now)):
                out.append(("pairing", parent, None, sid, a["name"]))
            elif result:
                out.append(result)
            elif a["placement_rule"] in ("launch", "pairing"):
                out.append((None, None, None, sid, a["name"]))  # no longer applies
        return out

    def launcher(self, names, a, s, agents, declared, team):
        """Rule "launch": the agent whose session ran the newest literal `herdr agent start <name>` before the agent was
        first seen and, when both name a pane, in the agent's pane. First seen is its session's first event; with no
        session, herdr's state_since (a start after it is another agent's). A start whose result the evidence holds as a
        failure is skipped. Only a launcher on the chart and in the agent's own team counts, so any other leaves the
        message, brief and pairing rules to run."""
        best, since = None, stamp(a["state_since"]) if not s and a["state_since"] else None
        for l in self.ev.launches.values():
            if l.name != a["name"] or (l.pane and a["pane"] and l.pane != a["pane"]) or (s and l.t > s.first + LAUNCH):
                continue
            if since and l.t > since or (r := self.ev.results.get(l.key[1])) and not r.ok:
                continue
            if best is None or l.t > best.t:
                best = l
        who = names.name(best.sid) if best else None
        if not who or who == a["name"] or not (who in declared or who in agents):
            return None
        d = declared.get(who)
        home = d.get("team") if d else self.structure.team_by_folder(agents[who]["cwd"]) or self.structure.team_by_workspace(agents[who]["workspace"])
        return who if home == team else None

    def first_rules(self, names, rows, pairs, now, a, sid, s, t, agents):
        """The message and brief rules. (True, None) when the stored result stands; else (False, the row to store or
        None), whose rule is None when neither gave a parent."""
        if not s:
            if a["placed_session"] is not None and a["placed_session"] != sid:
                return False, (None, None, None, None, a["name"])  # found for another session: no longer applies
            return a["placement_rule"] in ("message", "brief") and a["placed_by"] is not None, None  # the stored result stands
        if s.first >= now - WINDOW + PROMPT:
            ev, found = self.ev, first_reach(self.ev, rows, pairs, sid)
        elif a["placed_session"] == sid and a["placement_rule"] != "launch":
            return a["placement_rule"] in ("message", "brief") and a["placed_by"] is not None, None  # replayed before; the result is on the row
        else:
            ev, found = self.replay(sid, s.first)
            if found is False:
                return True, None
        rule = "message"
        # A message counts only when the message rule can use it (a sender in the agent's own workspace); any other
        # leaves the brief to be tried, so a message from another workspace never hides a valid brief.
        if not (found and found[1] != a["name"] and a["workspace"] and home(self.structure, agents, found[1]) == a["workspace"]):
            roots, briefs = team_roots(t)
            rule, found = "brief", briefed_by(ev, sid, roots, briefs)
        key, sender = found if found and found[1] != a["name"] else (None, None)
        return False, (rule if sender else None, sender, key, sid, a["name"])

    def replay(self, sid, first):
        """Placement replay: the stores' files modified since the session began, matched the same way."""
        ev = Evidence(first - PROMPT)
        if any(h[0] != "ok" for h in read_stores(ev, first - PROMPT).values()):
            return ev, False
        rows, pairs, _ = build(ev, Names(self.conn, ev, self.structure), time.time())
        return ev, first_reach(ev, rows, pairs, sid)

    def rebuild_gone(self, names, rows, now):
        """After a wipe only: unlisted sessions with an event in the last 24 hours that are not live come back as Gone,
        dated by their last transcript event. Kept to sessions that a source names or that exchanged a message with a
        named agent: any other session on the box may never have been a herdr agent (every session has a first prompt).
        False until herdr has given a snapshot."""
        if not self.conn.execute("SELECT 1 FROM source_health WHERE source = 'hc' AND snapshot IS NOT NULL").fetchone():
            return False
        live = {r["name"]: r["session_id"] for r in self.conn.execute(
            """SELECT a.name, a.session_id FROM agents a JOIN source_health h ON h.source = 'hc'
               AND a.snapshot = h.snapshot""")}
        ends = {r[s] for r in rows.values() for s, other in ((3, 8), (7, 4)) if r[s] and r[other]}
        declared = self.structure.declared()
        for sid, s in sorted(self.ev.sessions.items(), key=lambda x: -x[1].last):
            name = names.name(sid)
            if s.last < now - 24 * 3600 or sid in live.values() or name in live or name in declared:
                continue
            if name or sid in ends:
                self.conn.execute(
                    """INSERT OR IGNORE INTO agents (name, runtime, session_id, cwd, last_seen, rebuilt, name_lost)
                       VALUES (?, ?, ?, ?, ?, 1, ?)""",
                    (name or names.label(sid), s.runtime, sid, s.cwd, iso(s.last)[:19] + "Z", int(name is None)))
        return True
