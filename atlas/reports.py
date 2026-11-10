"""Each team's reports, from the reports folder structure.toml names: every *.md is statted, and read only when it
changed since the For Dylan rule's start, the team's STATUS links it, or an indexed work item names it. A read keeps the title, written time, author,
the two For Dylan blocks' labels as written and the first pull request link. Walked by descriptors, so no symlink is
followed; a file is read again only when its mtime or size changes."""
import json
import os
import re
import stat
from datetime import UTC, datetime

from . import db, status
from .structure import expand
from .files import open_below, open_dir, open_root, read_fd

# Dylan's For Dylan rule, Sat 10-31 1:32 PM ET: only reports changed since then are read, unless STATUS links one.
START = datetime(2026, 10, 31, 17, 32, tzinfo=UTC)
START_NS = int(START.timestamp()) * 10**9
MAX = 1 << 20  # a larger file is skipped and counted
# The two blocks of records/specs/for-dylan-blocks.md, by their exact heading, and each one's labels.
BLOCKS = {
    "## For Dylan": ("change", ("What changed for a user", "Why", "How it was checked", "Not checked", "The code",
                                "Data", "Screens")),
    "## For Dylan: ask": ("ask", ("Question", "Background", "Data", "Look at", "Options", "Recommended", "Evidence", "Default",
                                  "Cost of being wrong", "Asked")),
}
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")
CLOCK = re.compile(r"(?P<h>\d{1,2}):(?P<min>\d{2}) (?P<ap>[AP]M) ET\b"
                   r"(?:\s*\((?P<uh>\d{1,2}):(?P<umin>\d{2}) UTC(?P<utail>[^)]*)\))?")
DATE = re.compile(r"(?<![\d-])(?:(?P<y>\d{4})-)?(?P<m>\d{1,2})-(?P<d>\d{1,2})(?![\d-])")
DAY = re.compile(r"(\d{4}-\d{2}-\d{2})")
# A STATUS link to a report, taken in this order, each span masked once taken: a whole Markdown link (its target), a
# backticked path; then URLs are masked, and what is left may hold a bare path (trailing .,;:) dropped), which counts
# only when it starts with /, ~/ or ./ or holds a /.
MD_LINK = re.compile(r"(?:\[[^\]]*)?\]\(([^)\s]+\.md)\)")
TICKED = re.compile(r"`([^`\s]+\.md)`")
URL = re.compile(r"(?<![\w+.-])[A-Za-z][\w+.-]*://\S*")  # a scheme starts a word, so a long word is tried once
BARE = re.compile(r"(?<![^\s(\[])([^\s`()\[\]<>\"']+\.md)[.,;:)]*(?=\s|$)")
PR = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def fenced(lines):
    """For each line, whether it is a code fence or inside one. A fence opens with three or more ` or ~ (a ` fence's
    info text holds no `) and closes only on the same character, at least as many, with nothing after them."""
    out, opened = [], None
    for line in lines:
        m = FENCE.match(line)
        if opened is None and m and not (m[1][0] == "`" and "`" in m[2]):
            opened = m[1]
        elif opened and m and m[1][0] == opened[0] and len(m[1]) >= len(opened) and not m[2].strip():
            out.append(True)
            opened = None
            continue
        out.append(opened is not None)
    return out


def skipped(name):
    """A hidden entry or a *.key, never opened or entered, by the walk or through a link."""
    return name.startswith(".") or name.endswith(".key")


def roots(t):
    """A team's reports folder, then its folders, then its items folder, ~ expanded and normalised as spelt (no symlink
    resolved). These are the roots a team's files are read from."""
    spelt = [t.get("reports")] + list(t.get("folders", [])) + [t.get("items")]
    return [os.path.normpath(expand(r)) for r in spelt if isinstance(r, str) and r]


def inside(path, among):
    """(root, relative path) for an allowed path under a root; excluded components never reach a reader."""
    for root in among:
        if path.startswith(root.rstrip("/") + "/"):
            relative = path[len(root.rstrip("/")) + 1:]
            if any(skipped(part) or not part for part in relative.split("/")):
                return None
            return root, relative
    return None


def walk(root, suffixes=".md"):
    """(path relative to root, lstat) of every regular file below root ending in suffixes (*.md by default), and how many folders below it could not be
    listed. Each folder is opened with O_NOFOLLOW from its parent's descriptor, so no symlink is entered; hidden entries
    and *.key are never opened. Raises OSError when root itself cannot be opened."""
    found, failed = [], 0

    def down(fd, prefix):
        nonlocal failed
        with os.scandir(fd) as it:
            entries = []
            for e in it:
                if skipped(e.name):
                    continue
                try:
                    entries.append((e.name, e.stat(follow_symlinks=False)))
                except OSError:
                    failed += 1
        for name, st in entries:
            if stat.S_ISREG(st.st_mode) and name.endswith(suffixes):
                found.append((prefix + name, st))
            elif stat.S_ISDIR(st.st_mode):
                try:
                    sub = open_dir(name, fd)
                except OSError:
                    failed += 1
                    continue
                try:
                    down(sub, prefix + name + "/")
                except OSError:
                    failed += 1
                finally:
                    os.close(sub)

    fd = open_root(root)
    try:
        down(fd, "")
    finally:
        os.close(fd)
    return found, failed


def written(preamble, filename, front, modified):
    """When the report was written and how that was found, never guessed. First a clock time in the preamble (its
    metadata lines: see parse), on a line containing "written" (from that word on), else on the From line, with the date
    nearest it on its line, outside the UTC bracket, else the file name's; the UTC bracket, when present, must agree.
    A block's text or a fenced sample never counts. Then a frontmatter date, then the file name's date, each as the
    date alone."""
    named = DAY.match(filename)
    # A line that says written wins; the From line's clock counts only when no such line has one.
    tries = [(l, l.lower().find("written")) for l in preamble if "written" in l.lower()]
    tries += [(l, 0) for l in preamble if l.startswith("From ") and "written" not in l.lower()]
    clock, line = next(((c, l) for l, at in tries if (c := CLOCK.search(l, at))), (None, None))
    if clock:
        dates = [d for d in DATE.finditer(line) if not clock.start() <= d.start() < clock.end()]
        near = min(dates, key=lambda d: d.start() - clock.end() if d.start() > clock.start() else clock.start() - d.end(),
                   default=None)
        y, m, d = (near["y"], near["m"], near["d"]) if near else named[1].split("-") if named else (None,) * 3
        y = y or (named[1][:4] if named else None)
        # The whole bracket, so its weekday or date is checked as in decisions; any other text there refuses the time.
        bracket = f" ({clock['uh']}:{clock['umin']} UTC{clock['utail']})" if clock["uh"] else ""
        if m and (at := status.written(f"{y + '-' if y else ''}{m}-{d} {clock['h']}:{clock['min']} {clock['ap']} ET"
                                       f"{bracket}", modified)):
            return at, "header"
    if DAY.match(front.get("date", "")):
        return DAY.match(front["date"])[1], "frontmatter"
    if named:
        return named[1], "filename"
    return None, "none"


def labels(lines, names, fence):
    """Each label's Markdown text as written, from after its bold label to the next label, keyed by the label. A label
    counts at the start of a line or a bullet, outside a code fence (fence: one flag per line); one not written, or
    written with no text, is absent; a repeat is ignored with its text."""
    pattern = re.compile(r"(?:[-*] )?\*\*(" + "|".join(map(re.escape, names)) + r")\.\*\*(.*)$")
    found, current = {}, None
    for line, f in zip(lines, fence):
        m = None if f else pattern.match(line)
        if m:
            current = m[1] if m[1] not in found else None
            if current:
                found[current] = [m[2]]
        elif current:
            found[current].append(line)
    return {k: text for k, v in found.items() if (text := "\n".join(v).strip())}


def parse(text, filename, modified):
    """A report's fields. Headings inside code fences are not headings."""
    lines = text.splitlines()
    front = {}
    if lines and lines[0].strip() == "---":
        end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() in ("---", "...")), None)
        if end is not None:
            for l in lines[1:end]:
                if m := re.match(r"([A-Za-z_][\w-]*):\s*(.*)$", l):
                    front.setdefault(m[1].lower(), m[2].strip().strip("'\""))
            lines = lines[end + 1:]
    fence = fenced(lines)
    title = next((l[2:].strip() for l, f in zip(lines, fence) if not f and l.startswith("# ")), None)
    header = lines[:15]
    author = next((l.strip() for l in header if l.startswith("From ")), None) or front.get("author") or None
    # The preamble: the header lines before the first ## heading, outside code fences.
    first = next((i for i, l in enumerate(lines) if not fence[i] and l.startswith("## ")), len(lines))
    at, src = written([l for l, f in zip(header[:first], fence) if not f], filename, front, modified)
    blocks = {}
    for i, l in enumerate(lines):
        if not fence[i] and l.rstrip() in BLOCKS and BLOCKS[l.rstrip()][0] not in blocks:
            kind, names = BLOCKS[l.rstrip()]
            end = next((j for j in range(i + 1, len(lines)) if not fence[j] and re.match(r"#{1,2} ", lines[j])),
                       len(lines))
            blocks[kind] = labels(lines[i + 1:end], names, fence[i + 1:end])
    asked = blocks.get("ask", {}).get("Asked")
    pr = PR.search(text)
    return {"title": title or filename.removesuffix(".md"), "written_at": at, "written_src": src, "author": author,
            "has_change": "change" in blocks, "has_ask": "ask" in blocks,
            "change_labels": json.dumps(blocks["change"], ensure_ascii=False) if "change" in blocks else None,
            "ask_labels": json.dumps(blocks["ask"], ensure_ascii=False) if "ask" in blocks else None,
            "asked_at": status.written(asked.strip(" `*"), modified) if asked else None,
            "pr": pr[0] if pr else None}


def linked(t, among):
    """{absolute path: the STATUS line as written} for each *.md the team's STATUS links, in backticks, as a Markdown
    link or as a bare path (see MD_LINK), on its Waiting on Dylan line or in its ## For Dylan section, outside code fences. A relative path is tried from
    the STATUS file's folder, then from each root; it counts only as a regular file inside one of the team's roots with
    no hidden or *.key component below the root."""
    source = expand(t["status"])
    with open(source, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    # Fenced lines are blanked in place, so the first 20 lines are the same physical lines status.three reads.
    lines = ["" if inside_fence else l for l, inside_fence in zip(lines, fenced(lines))]
    waiting = next((l for l in lines[:20] if (m := status.LINE.match(l)) and status.KIND[m[1].lower()] == "waiting"), None)
    section, on = [], False
    for l in lines:
        if l.startswith(("# ", "## ")):
            on = l.rstrip() == "## For Dylan"
        elif on:
            section.append(l)
    out = {}
    for line in ([waiting] if waiting else []) + section:
        tokens = []
        rest = MD_LINK.sub(lambda m: tokens.append(m[1]) or " " * len(m[0]), line)
        rest = TICKED.sub(lambda m: tokens.append(m[1]) or " " * len(m[0]), rest)
        rest = URL.sub(lambda m: " " * len(m[0]), rest)
        tokens += [t for t in BARE.findall(rest) if t.startswith(("/", "~/", "./")) or "/" in t]
        for token in tokens:
            if token.startswith("~") and not token.startswith("~/"):  # never another account's home
                continue
            token = expand(token)
            bases = [""] if os.path.isabs(token) else [os.path.dirname(source), *among]
            for base in bases:
                path = os.path.normpath(os.path.join(base, token))
                try:
                    regular = stat.S_ISREG(os.lstat(path).st_mode)
                except OSError:
                    continue
                if regular and (found := inside(path, among)) and not any(map(skipped, found[1].split("/"))):
                    out.setdefault(path, line)
                    break
    return out


class ReportsPoller:
    interval = 30

    def __init__(self, conn, structure, review=None):
        self.conn, self.structure, self.review = conn, structure, review
        self.loaded = set()  # reread once at startup so derived labels follow the current parser
        self.scans = {}  # path -> (mtime_ns, size, review.scan of it): an old report is read once to see who it names

    def named(self, team, path, root, rel, st):
        """Whether a report older than START names an open change of this team (review.names): read once, by
        descriptor inside the team's roots, bounded by MAX, and read again only when its mtime or size changes."""
        if self.review is None or st.st_size > MAX or not self.review.has_changes(team):
            return False
        hit = self.scans.get(path)
        if hit is None or hit[:2] != (st.st_mtime_ns, st.st_size):
            try:
                data = read_fd(open_below(root, rel), MAX + 1)
            except OSError:
                return False
            from . import review as rv
            hit = self.scans[path] = (st.st_mtime_ns, st.st_size, rv.scan(data[:MAX].decode("utf-8", errors="replace"), self.review.repo_names()))
        return self.review.names(team, hit[2])

    def poll(self):
        if self.structure.mtime is None:  # not loaded yet, or its first read failed: an empty structure purges nothing
            return
        at = db.now()
        teams = {k: t for k, t in self.structure.data.get("teams", {}).items()
                 if isinstance(t.get("reports"), str) and t["reports"]}
        with self.conn:
            self.conn.execute("DELETE FROM reports WHERE team NOT IN (SELECT value FROM json_each(?))",
                              (json.dumps(list(teams)),))
            for team, t in teams.items():
                self.team(team, t, at)

    def team(self, team, t, at):
        source, among = f"reports:{team}", roots(t)
        folder, errors = among[0], []
        try:
            found, failed = walk(folder)
        except OSError as e:  # the folder is missing or not a plain folder: its rows stay as they were
            db.set_health(self.conn, source, at, "failing", f"{t['reports']}: {e.strerror or e}")
            return
        if failed:
            errors.append(f"{failed} folders or entries below {t['reports']} could not be listed")
        try:
            links = linked(t, among) if isinstance(t.get("status"), str) and t["status"] else {}
        except OSError as e:
            links = {}
            errors.append(f"{t['status']}: {e.strerror or e}")
        files = {os.path.join(folder, rel): (folder, rel, st) for rel, st in found}
        stored = {r[0]: (r[1], r[2]) for r in self.conn.execute(
            "SELECT path, mtime_ns, size FROM reports WHERE team = ?", (team,))}
        # Linked files outside the walk, and stored rows the walk did not meet (a linked file outside the reports folder).
        for path in (links.keys() | stored.keys()) - files.keys():
            if (found_in := inside(path, among)) is None or any(map(skipped, found_in[1].split("/"))):
                continue
            try:
                st = os.lstat(path)
            except (FileNotFoundError, NotADirectoryError) as e:  # gone: an unlinked row goes with it
                if path in links:
                    errors.append(f"{path}: {e.strerror or e}")
                continue
            except OSError as e:  # unreadable is not gone: the pass fails and deletes nothing
                errors.append(f"{path}: {e.strerror or e}")
                continue
            if stat.S_ISREG(st.st_mode):
                files[path] = (*found_in, st)
        keep, big = set(), 0
        for path, (root, rel, st) in files.items():
            if st.st_mtime_ns < START_NS and path not in links:
                # An older report read while STATUS linked it keeps its row, unlinked, while its file is unchanged, so its
                # ask page can say it is no longer asked. It is never read again unless linked again.
                if stored.get(path) == (st.st_mtime_ns, st.st_size):
                    keep.add(path)
                    continue
                item_link = self.conn.execute("SELECT 1 FROM item_links WHERE ref = ? AND kind IN ('ask', 'report')",
                                              (path,)).fetchone()
                if not item_link and not self.named(team, path, root, rel, st):
                    continue
            if st.st_size > MAX:
                big += 1
                continue
            keep.add(path)  # a file that fails to read keeps its last row
            if path in self.loaded and stored.get(path) == (st.st_mtime_ns, st.st_size):
                continue
            try:
                fd = open_below(root, rel)
                st = os.fstat(fd)
                if st.st_size > MAX:
                    os.close(fd)
                    big += 1
                    keep.discard(path)
                    continue
                data = read_fd(fd, MAX + 1)  # bounded, even if the file grows after the fstat
            except OSError as e:
                errors.append(f"{path}: {e.strerror or e}")
                continue
            if len(data) > MAX:
                big += 1
                keep.discard(path)
                continue
            self.loaded.add(path)
            row = parse(data.decode("utf-8", errors="replace"), os.path.basename(path), st.st_mtime) | {
                "path": path, "team": team, "name": rel.removesuffix(".md"), "in_reports": root == folder,
                "mtime_ns": st.st_mtime_ns, "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
            self.conn.execute(f"INSERT OR REPLACE INTO reports ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                              tuple(row.values()))
        self.conn.executemany("UPDATE reports SET linked = ?, status_line = ? WHERE path = ?",
                              [(path in links, links.get(path), path) for path in keep])
        if not errors:  # only a complete read removes rows: a file that went, or an older one that changed while unlinked
            self.conn.execute("DELETE FROM reports WHERE team = ? AND path NOT IN (SELECT value FROM json_each(?))",
                              (team, json.dumps(sorted(keep))))
            db.set_health(self.conn, source, at)
        else:
            more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
            db.set_health(self.conn, source, at, "degraded", errors[0] + more, len(errors))
        self.conn.execute("UPDATE source_health SET skipped = ? WHERE source = ?", (big, source))
