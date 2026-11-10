"""The code review data: every open change in the repos structure.toml declares, read from the copies on this box with
read-only git (git.py), joined to the reports and STATUS that name it, and served by /api/review, /api/pr and
/api/pr-diff. Open means a local branch or refs/remotes/origin branch not merged into the repo's declared base, as of
the last fetch; a PR number comes only from a report or STATUS line that names it, else the change is addressed by
b-<first 7 of its head>. Kept in memory and rebuilt every 30 s; only the reviewed marks are kept in the database."""
import json
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from . import db, git, reports
from .structure import expand
from .files import open_below, open_dir, open_root, read_fd


class NotFound(Exception):
    pass


PRURL = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
PRNUM = re.compile(r"\bPR\s*#?(\d+)\b")
TICK = re.compile(r"`([^`\s]{3,100})`")
HEX = re.compile(r"[0-9a-f]{7,40}")
LINES, LINES_MAX = 1500, 5000
CONTEXT_MAX = 5000
DAY, WEEK = 24 * 3600, 7 * 24 * 3600


@lru_cache(1024)  # a report's text is read once and kept, so its refs are parsed once; no caller changes the result
def refs(text):
    """What a text names: [(repo or None, n)] pull requests, as (repo, n, ticks-on-that-line), and every backticked
    token. Read as written, never guessed."""
    pairs, ticks = [], set()
    for line in text.splitlines():
        tk = set(TICK.findall(line))
        ticks |= tk
        for m in PRURL.finditer(line):
            pairs.append((m[2].lower(), int(m[3]), tk))
        for m in PRNUM.finditer(line):
            pairs.append((None, int(m[1]), tk))
    return pairs, ticks


def scan(text, repos=()):
    """What of a report an open change could be named by, kept small: its pull request links as (repo, n), the
    backticked tokens that look like a branch name or a commit id, and which of the declared repos (lower case) it
    names in words."""
    pairs, ticks = refs(text)
    low = text.lower()
    return ([(r, n) for r, n, _ in pairs if r], {t for t in ticks if HEX.fullmatch(t) or (BRANCH.fullmatch(t) and not re.search(r"\.\w{1,5}$", t))},
            frozenset(r for r in repos if r in low))


def iso(t):
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def cid(c):
    return c["n"] if c["n"] is not None else "b-" + c["head"][:7]


BRANCH = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_./-]+")
LONG_LIVED = {"main", "master", "dev", "develop", "staging", "prod", "production"}


@lru_cache(4096)
def head_ids(head):
    """Every hex id a text could write that starts a head: its first 7 to 40 characters, when they are hex."""
    return frozenset(p for n in range(7, 41) if HEX.fullmatch(p := head[:n]))


def ticked(c, ticks, named=frozenset(), shared=None):
    """Whether tokens a text wrote in backticks name this change: its head id, or any ref name that points at its head
    (never a long-lived one). A ref name counts by repo, as in Review.names: when the text names repos (in words or in a
    pull request link), only a change of one of them; when it names none, only a ref name that one repo of the team
    has open (shared: {ref name: {repo}} over the team's changes). A name two repos share, with no repo named, names no
    change."""
    if not ticks.isdisjoint(head_ids(c["head"])):
        return True
    return any(a in ticks and a not in LONG_LIVED and ((c["repo"].lower() in named) if named else (shared or {}).get(a) == {c["repo"]})
               for a in c["aliases"])


def repos_named(text, declared):
    """Which of the declared repos (lower case) a text names, in words or in a link."""
    low = text.lower()
    return frozenset(d for d in declared if d in low)


class Memo:
    """A small LRU, keyed by object ids (which never change) and file stats."""
    def __init__(self, size):
        self.d, self.size, self.lock = OrderedDict(), size, threading.Lock()

    def get(self, key, make):
        with self.lock:
            if key in self.d:
                self.d.move_to_end(key)
                return self.d[key]
        value = make()
        with self.lock:
            self.d[key] = value
            while len(self.d) > self.size:
                self.d.popitem(last=False)
        return value


FILES, DIFFS, TEXTS = Memo(4096), Memo(256), Memo(1024)


def read_report(path, t):
    """A report's text through descriptors, or None; cached by mtime and size."""
    among = reports.roots(t)
    found = reports.inside(path, among)
    if not found:
        return None
    try:
        st = os.lstat(path)
        return TEXTS.get((path, st.st_mtime_ns, st.st_size), lambda: read_fd(open_below(*found), reports.MAX).decode("utf-8", "replace"))
    except OSError:
        return None


def collect(structure):
    """{repo: {team, base, fetched, errors, changes}} and the rows git gave, before joining to reports."""
    out = {}
    declared = {}  # {repo: [team, ...]} in structure.toml's order
    for team, t in structure.data.get("teams", {}).items():
        for r in t.get("repos", []):
            declared.setdefault(r["name"], []).append(team)
    for team, t in structure.data.get("teams", {}).items():
        for r in t.get("repos", []):
            name, errors, found, fetched, clones = r["name"], [], {}, [], []
            good, bad = git.declare(r.get("paths", []))
            errors.extend(bad)
            for path in good:
                p = path
                try:
                    # The base is the remote-tracking ref only, as of the last fetch: a local branch can be months stale.
                    base_id = git.resolve(path, f"refs/remotes/origin/{r['base']}")
                    if not base_id:
                        errors.append(f"{p}: origin/{r['base']} is not in this clone; Atlas does not fetch")
                        continue
                    ids, base_time = [base_id], git.commit_time(path, base_id)
                    clones.append((path, base_id))
                    wt = git.worktrees(path)
                    if (f := git.fetched_at(path)):
                        fetched.append(f)
                    for b in git.branches(path, ids):
                        if b["name"].startswith("archive/"):  # DevOps's cleanup backups are not work
                            continue
                        c = found.get(b["id"])
                        if c is None:
                            found[b["id"]] = c = {"repo": name, "team": team, "branch": b["name"], "head": b["id"], "time": b["time"],
                                                  "subject": b["subject"], "author": b["author"], "path": path, "base_ids": ids, "base_time": base_time,
                                                  "worktree": wt.get(b["name"]), "n": None, "aliases": {b["name"]}}
                            if len(declared[name]) > 1:
                                c["teams"] = declared[name]
                        c["aliases"].add(b["name"])
                except git.GitError as e:
                    errors.append(str(e))
            out[name] = {"clones": clones, "team": team, "base": r["base"], "fetched": max(fetched) if fetched else None,
                         "errors": errors, "changes": list(found.values())}
    return out


def stats(c):
    """Merge base, file list and totals for one change, cached by ids."""
    def make():
        try:
            base = git.merge_base(c["path"], c["base_ids"][0], c["head"])
            if base is None:
                return {"error": "no merge base with the declared base"}
            fs = git.files(c["path"], base, c["head"])
            return {"base": base, "files": fs}
        except git.GitError as e:
            return {"error": str(e)}
    return FILES.get((c["path"], c["base_ids"][0], c["head"]), make)


FIRSTS = Memo(4096)


def first(c):
    """The subject of the change's first commit after the base (the oldest on the branch), or None; cached by ids."""
    def make():
        try:
            return git.first_subject(c["path"], c["base_ids"][0], c["head"])
        except git.GitError:
            return None
    return FIRSTS.get((c["path"], c["base_ids"][0], c["head"]), make)


LANDED = Memo(8192)


def landed(c):
    """Whether the change's content is already in origin/<base> (landed by squash or otherwise), so it is not open."""
    s = stats(c)
    if "error" in s:
        return False
    paths = sorted({p for f in s["files"] for p in (f["path"], f["old"]) if p})
    try:
        return LANDED.get((c["path"], c["base_ids"][0], c["head"]), lambda: git.landed(c["path"], c["base_ids"][0], c["head"], paths))
    except git.GitError:
        return False


def review_names(root, parts, n):
    """The review-file names in one folder (below a root, by descriptors, no symlink followed): *-pr<n>-review*.md, not
    hidden and not a *-brief.md. Empty when the folder cannot be opened."""
    fds = []
    try:
        fds.append(open_root(root))
        for part in parts:
            fds.append(open_dir(part, fds[-1]))
        with os.scandir(fds[-1]) as it:
            return [e.name for e in it if e.is_file(follow_symlinks=False) and not e.name.startswith(".") and not e.name.endswith("-brief.md")
                    and re.fullmatch(r"[^/]*-pr%d-review[^/]*\.md" % n, e.name)]
    except OSError:
        return []
    finally:
        for fd in fds:
            os.close(fd)


def findings_file(structure, c):
    """The review file of the change's team, or for a repo two teams declare, the first one found among the declaring
    teams in declared order (Atlas never picks one team before looking in all)."""
    for team in c.get("teams") or [c["team"]]:
        if (hit := team_findings(structure, c, team)):
            return hit
    return None


def team_findings(structure, c, team):
    """(root, relative path) of the review file a team keeps for this PR, or None: *-pr<n>-review*.md in records/reviews
    beside its decisions file or in its reports folder, inside one of the team's declared roots, newest name first. When
    the team declares more than one repo, only a file whose text names this repo (its name as a whole token, which a
    GitHub URL holds too) counts, since two repos can share a PR number. Listed and opened by descriptors, so no
    symlink is followed, in a folder or a file."""
    t = structure.data["teams"][team]
    if c["n"] is None:
        return None
    among, folders = reports.roots(t), []
    if isinstance(t.get("decisions"), str):
        folders.append(os.path.normpath(os.path.join(os.path.dirname(expand(t["decisions"])), "reviews")))
    if isinstance(t.get("reports"), str) and t["reports"]:
        folders.append(os.path.normpath(expand(t["reports"])))
    found = []
    for folder in dict.fromkeys(folders):
        hit = reports.inside(folder + "/x", among)
        if hit:
            root, parts = hit[0], hit[1].split("/")[:-1]  # the probe's last part is the "x" added above
            found += [(name, root, "/".join([*parts, name])) for name in review_names(root, parts, c["n"])]
    many = len(t.get("repos", [])) > 1
    named = re.compile(r"(?<![\w.-])" + re.escape(c["repo"]) + r"(?![\w-])", re.I)
    for name, root, rel in sorted(found, reverse=True):
        if not many:
            return root, rel
        try:
            if named.search(review_text((root, rel))[0]):
                return root, rel
        except OSError:
            continue
    return None


def review_text(found):
    """(text, cut): a review file's text, opened below its root without following a symlink, at most reports.MAX bytes;
    cut is True when the file held more than that. Cached by the file's size and time."""
    fd = open_below(*found)
    try:
        st = os.fstat(fd)
        key = ("txt", os.path.join(*found), st.st_mtime_ns, st.st_size)
        with TEXTS.lock:
            hit = TEXTS.d.get(key)
        if hit is None:
            f, fd = fd, None  # read_fd closes it
            def make():
                data = read_fd(f, reports.MAX + 1)
                return data[:reports.MAX].decode("utf-8", "replace"), len(data) > reports.MAX
            hit = TEXTS.get(key, make)
        return hit
    finally:
        if fd is not None:
            os.close(fd)


FINDING = re.compile(r"\*\*([BSN]\d+)\. (.+?)\*\* File: `([^`:]+):(\d+)`(?:, especially line (\d+))?[^\n]*\n\n(.+?)(?=\n\n\*\*[BSN]\d+\. |\n\n##? |\n\nVerification|\Z)", re.S)
HEADER = re.compile(r"^\*\*[BSN]\d+\. ", re.M)


WORDS = {w: i for i, w in enumerate("one two three four five six seven eight nine ten".split(), 1)}
NUM = r"(\d+|" + "|".join(WORDS) + r")"
LEAD = re.compile(r"(?:no blocking finding|no blockers)\.\s+" + NUM + r"\s+S\s+findings?\s+(?:should|must) be fixed\b", re.I)
# After that clause nothing may restate the state: no other S or B count or mention, nothing fixed, remaining or past.
OTHER = re.compile(r"\b(?:fixed|remain\w*|were|blockers?|blocking)\b|\bB\d*\b|\bS\s+findings?\b", re.I)


def verdict_counts(paragraph):
    """{should_fix: n} only for a Verdict paragraph (label and bold marks removed) that opens with exactly "No blocking
    finding." or "No blockers." (any case), continues "<n> S finding(s) should be fixed" or "must be fixed", n a number
    or a word from one to ten, and says nothing after that about fixed, remain, were, a blocker, a B finding or another S
    count. Anything else is None: never guessed."""
    text = " ".join((paragraph or "").replace("*", "").removeprefix("Verdict:").split())
    m = LEAD.match(text)
    if not m or OTHER.search(text[m.end():]):
        return None
    n = int(m[1]) if m[1].isdigit() else WORDS[m[1].lower()]
    return {"should_fix": n} if 1 <= n <= 10 else None


def parse_findings(text, cut=False):
    """Strict: only a finding written as **S1. Title.** File: `path:line`[, especially line N] with its paragraph; a
    header that does not read that way is counted as unparsed. fixed is set only by '**S1 fixed**' in the text."""
    start = re.search(r"Review complete at `([0-9a-f]{7,40})`", text)
    found = [{"id": m[1], "title": m[2].rstrip("."), "path": m[3], "line": int(m[5] or m[4]), "text": m[6].strip(),
              "fixed": re.search(rf"\*\*{m[1]} fixed\*\*", text) is not None} for m in FINDING.finditer(text)]
    paras = [p.strip() for p in text.split("\n\n")]
    verdict = next((p for p in paras if p.startswith("**Verdict:**")), None)
    if verdict:
        body = " ".join(verdict.removeprefix("**Verdict:**").replace("**", "").split())
        summary = body if len(body) <= 300 else body[:300].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    else:
        summary = next((p for p in paras[1:] if p and not p.startswith(("#", "**"))), None)
    unparsed = len(HEADER.findall(text)) - len(found)
    # read is true only when a finding parsed strictly, no header was left unparsed and the file was not cut at the size
    # cap: a completion marker alone, a "### S1." header or a malformed "**S1.**" header is "not read", never zero.
    return {"at": start[1] if start else None, "summary": summary, "findings": found, "unparsed": unparsed,
            "verdict": verdict_counts(verdict),
            "read": bool(found) and not unparsed and not cut}


def attach(snap, structure, conn):
    """Join the changes to the reports and STATUS that name them: PR number, story report, asks and waiting lines."""
    rows = [dict(r) for r in conn.execute(
        "SELECT team, name, path, title, written_at, modified, author, has_change, has_ask, linked, ask_labels, asked_at FROM reports")]
    status = {r["team"]: dict(r) for r in conn.execute("SELECT team, waiting, modified FROM team_status")}
    asks = []
    for r in sorted(rows, key=lambda r: r["written_at"] or r["modified"] or "", reverse=True):
        t = structure.data.get("teams", {}).get(r["team"])
        txt = read_report(r["path"], t) if t else None
        r["refs"] = refs(txt) if txt else ([], set())
        r["named"] = repos_named(txt, {n.lower() for n in snap}) if txt else frozenset()
    # What the reports say about each pull request link, for a change that is not on this box.
    declared, named = {n.lower(): n for n in snap}, {}
    for r in rows:
        for repo, n, tk in r["refs"][0]:
            if repo in declared:
                branch = next((t for t in sorted(tk) if BRANCH.fullmatch(t) and not re.search(r"\.\w{1,5}$", t)), None)
                commit = next((t for t in sorted(tk) if HEX.fullmatch(t) and re.search(r"\d", t)), None)
                named.setdefault((declared[repo], n), {"branch": branch, "commit": commit, "report": {"team": r["team"], "name": r["name"]}})
    by_team, allc = {}, []
    for repo in snap.values():
        for c in repo["changes"]:
            allc.append(c)
            # A change in a repo two teams declare is matched against each declaring team's reports, in declared order.
            for team in c.get("teams") or [repo["team"]]:
                by_team.setdefault(team, []).append(c)
    evidence = {id(c): [] for c in allc}
    for team, cs in by_team.items():
        shared = {}
        for c in cs:
            for a in c["aliases"] - LONG_LIVED:
                shared.setdefault(a, set()).add(c["repo"])
        for r in (r for r in rows if r["team"] == team):  # newest first
            pairs, ticks = r["refs"]
            for c in cs:
                if not ticked(c, ticks, r["named"], shared):
                    continue
                near = [n for repo, n, tk in pairs if repo in (None, c["repo"].lower()) and ticked(c, tk, r["named"], shared)]
                distinct = {n for repo, n, _ in pairs if repo in (None, c["repo"].lower())}
                n = near[0] if near else next(iter(distinct)) if len(distinct) == 1 else None
                evidence[id(c)].append((r, n))
    # The number, the rivals and the story are decided once per change over every declaring team's evidence together.
    for c in allc:
        c["n"] = next((n for _, n in evidence[id(c)] if n is not None), None)
    # Two heads of one branch name can claim one number: it stays with the head a report names by id, else the newest.
    for c in allc:
        rivals = [d for d in allc if d is not c and d["repo"] == c["repo"] and d["n"] == c["n"] and c["n"] is not None]
        if rivals:
            id_named = lambda x: any(HEX.fullmatch(t) and x["head"].startswith(t) for r, _ in evidence[id(x)] for t in r["refs"][1])
            best = max([c, *rivals], key=lambda x: (id_named(x), x["time"]))
            for x in [c, *rivals]:
                if x is not best:
                    x["n"] = None
    # A report that links the change's pull request names it too, with no branch or commit written.
    for c in allc:
        for r in (r for t in c.get("teams") or [c["team"]] for r in rows if r["team"] == t):
            if c["n"] is not None and any(repo == c["repo"].lower() and n == c["n"] for repo, n, _ in r["refs"][0]) \
                    and all(x is not r for x, _ in evidence[id(c)]):
                evidence[id(c)].append((r, c["n"]))
        story = next((r for r, _ in evidence[id(c)] if r["has_change"]), None)
        if story is None and c["n"] is not None:  # no For Dylan block: the newest report that links this pull request
            story = next((r for r, _ in evidence[id(c)] if any(repo == c["repo"].lower() and n == c["n"] for repo, n, _ in r["refs"][0])), None)
        c["story"] = {"team": story["team"], "name": story["name"], "title": story["title"], "author": story["author"]} if story else None
        c["named_by"] = [{"team": r["team"], "name": r["name"]} for r, _ in evidence[id(c)]]
        c["waits"] = []
    for team, cs in by_team.items():
        mine = [r for r in rows if r["team"] == team]
        shared = {}
        for c in cs:
            for a in c["aliases"] - LONG_LIVED:
                shared.setdefault(a, set()).add(c["repo"])
        repos_in = {c["repo"] for c in cs}

        def bare(text_, pairs, ticks, named_):
            """The changes a text names: by branch or commit, by pull request link, or by 'PR n' when only one of the team's
            repos has that number (or the text names the repo)."""
            low, hit = text_.lower(), []
            for c in cs:
                n_ok = c["n"] is not None and any(n == c["n"] and (repo == c["repo"].lower() or (repo is None and (
                    len({d["repo"] for d in cs if d["n"] == c["n"]}) == 1 or c["repo"].lower() in low))) for repo, n, _ in pairs)
                if ticked(c, ticks, named_, shared) or n_ok:
                    hit.append(c)
            return hit
        for r in mine:
            if r["linked"] and r["has_ask"]:
                q = json.loads(r["ask_labels"] or "{}").get("Question") or r["title"]
                for c in bare(r["title"] or "", *r["refs"], r["named"]):
                    c["waits"].append({"kind": "ask", "text": q, "since": r["asked_at"] or r["written_at"] or r["modified"],
                                       "report": {"team": team, "name": r["name"]}})
        s = status.get(team)
        if s and s["waiting"]:
            pairs, ticks = refs(s["waiting"])
            for c in bare(s["waiting"], pairs, ticks, repos_named(s["waiting"], {n.lower() for n in snap})):
                c["waits"].append({"kind": "waiting", "text": s["waiting"], "since": since_waiting(conn, team, s), "report": None})
    return named


def since_waiting(conn, team, s):
    r = conn.execute("SELECT first_seen FROM observations WHERE team = ? AND kind = 'waiting' AND value = ? ORDER BY id DESC LIMIT 1",
                     (team, s["waiting"])).fetchone()
    return (r["first_seen"] if r and r["first_seen"] else None) or s["modified"]


def totals(c):
    s = stats(c)
    if "error" in s:
        return {"error": s["error"]}
    fs = s["files"]
    return {"files": len(fs), "add": sum(f["add"] or 0 for f in fs), "del": sum(f["del"] or 0 for f in fs)}


class Review:
    interval = 30

    def __init__(self, conn, structure):
        self.conn, self.structure = conn, structure
        self.snap, self.lock = None, threading.Lock()
        self.indexed = {}

    def poll(self):
        if self.structure.mtime is None:
            return
        snap = collect(self.structure)
        allc = [c for r in snap.values() for c in r["changes"]]
        with ThreadPoolExecutor(8) as pool:
            done = dict(zip(map(id, allc), pool.map(landed, allc)))
        for r in snap.values():
            r["landed"] = [{"branch": c["branch"], "head": c["head"]} for c in r["changes"] if done[id(c)]]
            r["changes"] = [c for c in r["changes"] if not done[id(c)]]
        named = attach(snap, self.structure, self.conn)
        allc = [c for r in snap.values() for c in r["changes"]]
        with ThreadPoolExecutor(8) as pool:
            for c, tot, fs in zip(allc, pool.map(totals, allc), pool.map(first, allc)):
                c["totals"], c["first"] = tot, fs
        self.snap = {"at": db.now(), "repos": snap, "named": named}

    def repo_names(self):
        """The declared repos, lower case, as of the last poll."""
        return {r.lower() for r in self.snap["repos"]} if self.snap else set()

    def index(self, team):
        """What names this team's open changes, built once per poll: {branch: {repo}}, {7 hex of a head: [changes]},
        {(repo, n): change}."""
        snap = self.snap
        if self.indexed.get(team, (None,))[0] is not snap:
            branches, heads, numbers = {}, {}, set()
            for r in (snap["repos"].values() if snap else ()):
                for c in r["changes"] if r["team"] == team else ():
                    for a in c["aliases"] - LONG_LIVED:
                        branches.setdefault(a, set()).add(c["repo"])
                    heads.setdefault(c["head"][:7], []).append(c)
                    if c["n"] is not None:
                        numbers.add((c["repo"].lower(), c["n"]))
            self.indexed[team] = (snap, branches, heads, numbers)
        return self.indexed[team][1:]

    def names(self, team, found):
        """Whether a scan() of a report names an open change of this team: by its pull request link (the number a report
        already gave it), by a commit id written in backticks (a head's first 7 or more hex digits), or by a branch name
        written in backticks: of a repo the report names, or, when it names no repo, of the one repo that has it open
        across the team. A branch name the team's repos share, with no repo named, matches nothing; neither does one
        that belongs to a repo other than the ones the report names."""
        branches, heads, numbers = self.index(team)
        pairs, ticks, mentioned = found
        if numbers.intersection(pairs):
            return True
        for t in ticks:
            if HEX.fullmatch(t) and any(c["head"].startswith(t) for c in heads.get(t[:7], ())):
                return True
            repos = branches.get(t)
            # A report that names repos names a change's only through them; one that names none needs a branch that
            # only one repo of the team has open.
            if repos and (({r.lower() for r in repos} & mentioned) if mentioned else len(repos) == 1):
                return True
        return False

    def has_changes(self, team):
        return bool(self.snap) and any(r["team"] == team and r["changes"] for r in self.snap["repos"].values())

    def get(self):
        if self.snap is None:
            with self.lock:
                if self.snap is None:
                    self.poll()
        if self.snap is None:
            raise NotFound
        return self.snap

    def find(self, repo, n):
        r = self.get()["repos"].get(repo)
        for c in r["changes"] if r else ():
            if str(cid(c)) == str(n):
                return c
        raise NotFound


def marks(conn, login):
    if not login:
        return set()
    # By repo and exact head: the mark stays when a change gains its pull request number (its displayed id changes).
    return {(r["repo"], r["head"]) for r in conn.execute("SELECT repo, head FROM review_marks WHERE login = ?", (login,))}


def codex(app, c):
    """Codex's findings for this change from its team's review file, or None. Opened without following a symlink, at
    most reports.MAX bytes, parsed again only when the file's size or time changed."""
    found = findings_file(app.structure, c)
    if found is None:
        return None
    try:
        text, cut = review_text(found)
        key = ("rev", os.path.join(*found), len(text), hash(text), cut)
        parsed = TEXTS.get(key, lambda: parse_findings(text, cut))
    except OSError:
        return None
    return parsed | {"path": os.path.join(*found), "name": found[1].removesuffix(".md"), "current": bool(parsed["at"]) and c["head"].startswith(parsed["at"][:40])}


def row(app, c, reviewed):
    cx = codex(app, c)
    counts = {}
    for f in (cx or {"findings": []})["findings"]:
        counts[f["id"][0]] = counts.get(f["id"][0], 0) + 1
    return {"team": c["team"], **({"teams": c["teams"]} if "teams" in c else {}), "repo": c["repo"], "n": cid(c), "number": c["n"], "branch": c["branch"], "title": (c["story"] or {}).get("title") or c.get("first") or c["subject"],
            "subject": c["subject"], "first": c.get("first"), "head": c["head"], "head_time": iso(c["time"]), "author": c["author"], "totals": c["totals"], "story": c["story"],
            "codex": {"at": cx["at"], "current": cx["current"], "counts": counts, "summary": cx["summary"], "read": cx["read"], "verdict": cx["verdict"]} if cx else None,
            "reviewed": reviewed, "waits": c["waits"], "named": bool(c["named_by"] or c["waits"])}


def queue(app, conn, login, team_order, now=None):
    """The three groups of spec criterion 7, each change in exactly one: waits (named in an open ask or a Waiting
    line, oldest first), new (head committed in the last 24 h, not marked reviewed at that head; by team in the home
    page's order, newest first), and the rest, with per-repo counts that add up to the total."""
    snap, done = app.review.get(), marks(conn, login)
    now = now or time.time()
    waits, new, rest, repos = [], [], [], {}
    for name, r in snap["repos"].items():
        repos[name] = {"team": r["team"], "base": r["base"], "fetched": iso(r["fetched"]), "errors": r["errors"],
                       "open": 0, "landed": len(r["landed"]), "waits": 0, "new": 0, "rest": 0, "stale7": 0, "unnamed": 0}
        for c in r["changes"]:
            item = row(app, c, (name, c["head"]) in done)
            g = repos[name]
            g["open"] += 1
            g["stale7"] += now - c["time"] >= WEEK
            g["unnamed"] += not item["named"]
            if c["waits"]:
                waits.append(item); g["waits"] += 1
            elif now - c["time"] < DAY and not item["reviewed"]:
                new.append(item); g["new"] += 1
            else:
                rest.append(item); g["rest"] += 1
    waits.sort(key=lambda i: (min(w["since"] or "~" for w in i["waits"]), i["repo"], str(i["n"])))
    order = {t: k for k, t in enumerate(team_order)}
    new.sort(key=lambda i: -(datetime.fromisoformat(i["head_time"]).timestamp()))
    new.sort(key=lambda i: order.get(i["team"], len(order)))
    rest.sort(key=lambda i: (i["repo"], -datetime.fromisoformat(i["head_time"]).timestamp()))
    total = len(waits) + len(new) + len(rest)
    return {"as_of": snap["at"], "login": login, "total": total, "waiting_for_review": len(waits) + len(new),
            "waits": waits, "new": new, "rest": rest, "repos": repos}


def owner(app, conn, c, team=None):
    """The PR's owner: the agent its story report's From line names, only when Atlas's placement puts it in the change's
    team and it is live, else the team's lead; with its live state, so the page can turn Send off with a reason. A lead
    that is not live leaves Send off. Rechecked at send time, by the same call."""
    from . import web
    s = c["story"]
    worker = re.match(r"From `([^`]+)`", s["author"] or "") if s else None
    rows = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM agents")}
    live = {r["name"]: r["status"] for r in conn.execute(
        """SELECT a.name, a.status FROM agents a JOIN source_health h ON h.source = 'hc' AND a.snapshot = h.snapshot
           AND h.state = 'ok' WHERE a.pane IS NOT NULL""")}
    team = team or c["team"]
    lead = app.structure.lead(team)

    def team_of(name):
        d = app.structure.declared().get(name)
        return d.get("team") if d else web.place(rows[name], app.structure, rows)["team"] if name in rows else None
    name, is_worker = lead, False
    if worker and worker[1] in live and team_of(worker[1]) == team:
        name, is_worker = worker[1], True
    return {"name": name, "live": name in live, "state": None if name not in live else "blocked" if live[name] == "blocked" else "ready",
            "worker": is_worker, **({"team": team} if c.get("teams") else {})}


def absent(app, repo, n):
    """What to say for a change that is not open here, or NotFound when nothing names it: its branch is already in
    origin/<base>, or a report names its pull request and its head is not on this box (Atlas does not fetch)."""
    snap = app.review.get()
    r = snap["repos"].get(repo)
    if r is None:
        raise NotFound
    said = snap["named"].get((repo, int(n))) if str(n).isdigit() else None
    if said is None or not said["commit"]:
        raise NotFound
    found = None
    for path, base in r["clones"]:
        if git.resolve(path, said["commit"]):
            found = (path, base)
            break
    base_ref = "origin/" + r["base"]
    if found is None:
        at = f"{said['branch'] or 'its branch'} at {said['commit']}"
        return {"repo": repo, "n": n, "not_on_box": True, "branch": said["branch"], "commit": said["commit"], "story": said["report"],
                "message": f"This change is not on this box yet: {at}, from {said['report']['team']}/{said['report']['name']}. Atlas does not fetch."}
    path, base = found
    full = git.resolve(path, said["commit"])
    merged = git.merge_base(path, full, base) == full
    if merged or any(x["head"] == full for x in r["landed"]):
        return {"repo": repo, "n": n, "landed": True, "branch": said["branch"], "commit": said["commit"], "story": said["report"],
                "message": f"This change is already in {base_ref}: {said['commit']} is " + ("an ancestor of it." if merged else "landed by squash or otherwise.")}
    raise NotFound


def review_doc(app, repo, n):
    """{title, team, path, text} of the review file for this change, read-only, for /pr/<repo>/<n>/review. The file is the
    one findings_file picked: inside the team's roots, opened by descriptors, no symlink, at most reports.MAX bytes."""
    c = app.review.find(repo, n)
    found = findings_file(app.structure, c)
    if found is None:
        raise NotFound
    try:
        text = review_text(found)[0]
    except OSError:
        raise NotFound from None
    title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), os.path.basename(found[1]))
    return {"title": title, "team": c["team"], "repo": repo, "n": cid(c), "path": os.path.join(*found), "text": text}


def pr(app, conn, repo, n):
    """Meta, story, owner and findings for one change; a finding whose line is not in the diff is 'unchanged line'."""
    try:
        c = app.review.find(repo, n)
    except NotFound:
        return absent(app, repo, n)
    snap, s = app.review.get(), stats(c)
    cx = codex(app, c)
    finds = []
    for f in (cx["findings"] if cx else []):
        inside = False
        match = next((x for x in s.get("files", []) if x["path"] == f["path"]), None)
        if match and not match["binary"]:
            hunks = diff_of(c, s, match, 3)[0]
            inside = any(l[2] == f["line"] and l[0] != "-" for h in hunks for l in h["lines"])
        finds.append(f | {"unchanged_line": not inside})
    r = snap["repos"][repo]
    return {"repo": repo, "n": cid(c), "number": c["n"], "team": c["team"], **({"teams": c["teams"]} if "teams" in c else {}), "branch": c["branch"], "head": c["head"],
            "base": s.get("base"), "base_ref": "origin/" + r["base"], "base_time": iso(c["base_time"]), "head_time": iso(c["time"]), "author": c["author"], "subject": c["subject"],
            "worktree": c["worktree"], "totals": c["totals"], "error": s.get("error"),
            "as_of_fetch": iso(r["fetched"]), "repo_errors": r["errors"], "story": c["story"], "named_by": c["named_by"],
            "waits": c["waits"], "owner": owner(app, conn, c),
            **({"owners": [owner(app, conn, c, t) for t in c["teams"]]} if "teams" in c else {}),
            "codex": {k: cx[k] for k in ("path", "at", "current", "summary", "unparsed", "read", "verdict")} | {"findings": finds} if cx else None,
            "records": app.structure.data["teams"][c["team"]].get("name") or c["team"],
            **({"records_all": [app.structure.data["teams"][t].get("name") or t for t in c["teams"]]} if "teams" in c else {})}


def diff_of(c, s, f, context):
    return DIFFS.get((c["path"], s["base"], c["head"], f["path"], context), lambda: git.file_diff(c["path"], s["base"], c["head"], f, context))


def num(query, key, default, top):
    try:
        v = int(query.get(key, [default])[0])
    except ValueError:
        raise NotFound from None
    if not 0 <= v <= top:
        raise NotFound
    return v


def pr_diff(app, repo, n, query):
    """The change's files with "+N -M" and, bounded by ?lines= (default 1500, at most 5000) diff lines per request,
    hunks: for ?path= one file (?context= lines around each change, default 3, to expand; ?offset= to continue a
    cut file), or with ?hunks=1 the files in order from ?from= until the bound. All as git's base...head with -M."""
    try:
        c = app.review.find(repo, n)
    except NotFound:
        return absent(app, repo, n) | {"files": []}
    s = stats(c)
    if "error" in s:
        return {"repo": repo, "n": cid(c), "head": c["head"], "error": s["error"], "files": []}
    limit, context, offset = num(query, "lines", LINES, LINES_MAX), num(query, "context", 3, CONTEXT_MAX), num(query, "offset", 0, 10**9)
    out = {"repo": repo, "n": cid(c), "head": c["head"], "base": s["base"], "files": [dict(f, hunks=None) for f in s["files"]]}
    path = query.get("path", [None])[0]
    if path is not None:
        picked = [k for k, f in enumerate(out["files"]) if f["path"] == path]
        if not picked:
            raise NotFound
        start, only = picked[0], True
    elif query.get("hunks", ["0"])[0] == "1":
        start, only = num(query, "from", 0, 10**6), False
    else:
        return out | {"lines_max": limit}
    left, k = limit, start
    while k < len(out["files"]) and left > 0:
        f = out["files"][k]
        if f["binary"]:
            f["hunks"], f["total_lines"] = [], 0
        else:
            hunks, cut = diff_of(c, s, f, context)
            flat = [(i, l) for i, h in enumerate(hunks) for l in h["lines"]]
            take = flat[offset if only else 0:][:left]
            f["total_lines"], f["output_capped"] = len(flat), cut
            f["hunks"] = []
            for i, h in enumerate(hunks):
                ls = [l for j, l in take if j == i]
                if ls:
                    f["hunks"].append(h | {"lines": ls})
            left -= len(take)
            if (offset if only else 0) + len(take) < len(flat):
                out["next"] = {"path": f["path"], "offset": (offset if only else 0) + len(take)} | ({} if only else {"from": k + 1})
                break
        if only:
            break
        k += 1
        offset = 0
    if not only and k < len(out["files"]) and "next" not in out:
        out["next"] = {"from": k}
    return out | {"lines_max": limit}


def set_mark(conn, app, login, repo, n, head, reviewed):
    """Mark or unmark a change reviewed at the head given, which must be its current head."""
    c = app.review.find(repo, n)
    if head != c["head"]:
        raise NotFound
    with conn:
        conn.execute("DELETE FROM review_marks WHERE login = ? AND repo = ? AND head = ?", (login, repo, head))  # under any id it had
        if reviewed:
            conn.execute("INSERT INTO review_marks (login, repo, pr, head, at) VALUES (?, ?, ?, ?, ?)",
                         (login, repo, str(cid(c)), head, db.now()))


def compose(c, comments):
    """The one line a review sends: Dylan, review of <repo> PR <n>: <path>:<line>: <comment>; ..."""
    return f"Dylan, review of {c['repo']} PR {cid(c)}: " + "; ".join(f"{x['path']}:{x['line']}: {x['comment']}" for x in comments)
