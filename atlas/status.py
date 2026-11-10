"""Each team's STATUS and decisions file, as structure.toml names them. Read only, and only again when a file's
modified time changes: the three lines go to team_status, and what changed goes to observations, a kept table."""
import os
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from . import db
from .structure import expand

ET = ZoneInfo("America/New_York")
# A three-line label in any of the spellings measured: plain, `- ` and `**...:**`, in the file's first 20 lines.
LINE = re.compile(r"^[\s*-]*\**(working on|last shipped|waiting on (?:dylan|you))\**:\**\s*(.+)$", re.I)
KIND = {"working on": "working", "last shipped": "shipped", "waiting on dylan": "waiting", "waiting on you": "waiting"}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# An entry's leading stamp: a date then an ET clock time (decisions files), or a time then a date (Orion's STATUS),
# each with an optional weekday and an optional UTC time in brackets. Anything else between them, or after the ET
# time, is skipped rather than guessed. The one exception is up to three lowercase words between the date and the
# time ("answer arrived by", "read by", "about"), allowed only when a UTC bracket follows the ET time, since written()
# then confirms the time against it. The bracket is (H:MM UTC) or (HH:MM UTC), optionally followed by a weekday and a
# date, each after an optional comma, as written after midnight UTC: (01:23 UTC Sun), (01:15 UTC Sun 1 Nov),
# (01:15 UTC Sun, 1 Nov), (01:15 UTC 1 Nov), and Forge's month-day (02:09 UTC 10-23) or (02:09 UTC 10-23-2026).
UTC_BRACKET = (r"(?: \((?P<uh>\d{1,2}):(?P<umin>\d{2}) UTC(?:,? (?P<uday>[A-Z][a-z]{2})[a-z]*)?"
               r"(?:,? (?:(?P<ud>\d{1,2}) (?P<umon>[A-Z][a-z]{2})[a-z]*"
               r"|(?P<umm>\d{1,2})-(?P<udd>\d{1,2})(?:-(?P<uyy>\d{4}))?))?\))?(?! \()")
DATE_TIME = re.compile(r"(?:(?P<day>[A-Z][a-z]{2})[a-z]*,? )?(?:(?P<y>\d{4})-)?(?P<m>\d{1,2})-(?P<d>\d{1,2}),? "
                       r"(?:(?:[a-z]+ ){1,3}(?=\d{1,2}:\d{2} [AP]M ET \(\d{1,2}:\d{2} UTC))?(?P<h>\d{1,2}):(?P<min>\d{2}) (?P<ap>[AP]M) ET\b" + UTC_BRACKET)
TIME_DATE = re.compile(r"(?P<h>\d{1,2}):(?P<min>\d{2}) (?P<ap>[AP]M) ET\b" + UTC_BRACKET +
                       r" (?:(?P<day>[A-Z][a-z]{2})[a-z]*,? )?(?:(?P<y>\d{4})-)?(?P<m>\d{1,2})-(?P<d>\d{1,2})\b")


def three(lines):
    """The first working, shipped and waiting line in the first 20 lines, as written, and their line numbers."""
    out, at = {}, set()
    for i, line in enumerate(lines[:20]):
        m = LINE.match(line)
        if m and KIND[m[1].lower()] not in out:
            out[KIND[m[1].lower()]] = m[2].strip()
            at.add(i)
    return out, at


def entries(lines, skip=()):
    """Every line starting with `- ` at column 0, without the marker, except those at the line numbers in skip."""
    return [line[2:].strip() for i, line in enumerate(lines) if line.startswith("- ") and i not in skip and line[2:].strip()]


def written(entry, modified):
    """The entry's leading stamp as UTC text, or None unless it gives a date and an ET clock time cleanly: hour 1 to
    12, minutes 00 to 59. A date without a year takes the year that puts it on or before the file's modified date; a
    weekday must match it, and a UTC time in brackets (hour 0 to 23, minutes 00 to 59) must equal the ET time
    converted. The instant must read back in America/New_York as exactly the written date, hour and minute, so a
    clock time the spring change skips never counts; one the autumn change repeats counts only when its UTC bracket
    settles which of the two it is. A weekday or a day and month in that bracket must be the UTC instant's too."""
    text = entry.removeprefix("**")
    m = DATE_TIME.match(text) or TIME_DATE.match(text)
    if not m or not (1 <= int(m["h"]) <= 12 and int(m["min"]) <= 59):
        return None
    try:
        last = datetime.fromtimestamp(modified, ET)
        year = int(m["y"]) if m["y"] else last.year - ((int(m["m"]), int(m["d"])) > (last.month, last.day))
        hour = int(m["h"]) % 12 + (12 if m["ap"] == "PM" else 0)
        local = datetime(year, int(m["m"]), int(m["d"]), hour, int(m["min"]))
    except ValueError:
        return None
    if m["day"] and DAYS[local.weekday()] != m["day"].lower():
        return None
    # The two readings of the clock time, kept only when they read back as the written clock: none for a skipped
    # time, two for a repeated one, else one.
    readings = {u for f in (0, 1) if (u := local.replace(tzinfo=ET, fold=f).astimezone(UTC))
                .astimezone(ET).replace(tzinfo=None) == local}
    if m["uh"]:
        if int(m["uh"]) > 23 or int(m["umin"]) > 59:
            return None
        readings = {u for u in readings if (u.hour, u.minute) == (int(m["uh"]), int(m["umin"]))
                    and (not m["uday"] or DAYS[u.weekday()] == m["uday"].lower())
                    and (not m["ud"] or (u.day, MONTHS[u.month - 1]) == (int(m["ud"]), m["umon"].lower()))
                    and (not m["umm"] or (u.month, u.day) == (int(m["umm"]), int(m["udd"])))
                    and (not m["uyy"] or u.year == int(m["uyy"]))}
    return readings.pop().strftime("%Y-%m-%dT%H:%M:%SZ") if len(readings) == 1 else None


def decision_text(entry):
    """A decision as the page shows it, beside its own time: without the leading stamp, up to and including the comma
    that ends it, and without a trailing "Recorded by <name>.". A bold run opened before the stamp stays opened; a
    bold run made only of the stamp and a closing : or . goes whole, with the space after it. An entry whose stamp
    ends any other way, or that would be left empty or with its bold unbalanced, is shown as written."""
    bold = entry.startswith("**")
    text = entry.removeprefix("**")
    m = DATE_TIME.match(text) or TIME_DATE.match(text)
    if not m:
        return entry
    if text.startswith(", ", m.end()):
        rest, keep = text[m.end() + 2:], "**" if bold else ""
    elif bold and text[m.end():m.end() + 4] in (":** ", ".** "):
        rest, keep = text[m.end() + 4:], ""
    else:
        return entry
    rest = keep + re.sub(r"\s*Recorded by [^.*]+\.$", "", rest.rstrip())  # a name never holds Markdown
    # A strip that leaves nothing, or that unbalances the entry's bold, shows the entry as written.
    broken = rest.count("**") % 2 == 1 and entry.count("**") % 2 == 0
    return rest if rest.strip(" *") and not broken else entry


# Words Dylan is relaying are someone else's ("relaying"; "relayed by" is his own words passed on by an agent). A
# quote after one of these, past the relaying, is his own.
OWN = re.compile(r"his own answer|his answer|he answered|Dylan:")


def decision_news(entry):
    """A decision as the feed shows it, or None when it is not listed. Dylan's words are the first quote, skipping a
    question put to him (after "answering" or "in reply to"). A quote whose lead-in, back to the stamp or the quote
    before it, says Dylan is relaying it is not his: a later quote introduced as his answer is used instead, else the
    F1 text is shown. With an Effect: sentence the feed shows its first sentence (over 120 characters, cut at its first
    ; with a full stop) and then You: "<his words>"; without one, You: "<his words>" alone. Where that reading would
    mislead, the F1 text is shown: his first quote not introduced by a colon, a bold heading or "chose", a quote cut
    into parts, or an entry that says it is not a decision. An entry with no quote is not listed, nor is any result
    with unbalanced quotes or **, the F1 text included. The stored entry is never changed."""
    def listed(shown):
        return shown if shown.count('"') % 2 == 0 and shown.count("**") % 2 == 0 else None

    every = list(re.finditer(r'"([^"]+)"', entry))
    lead_in = {q.start(): entry[(every[i - 1].end() if i else 0):q.start()] for i, q in enumerate(every)}
    quotes = [q for q in every if not lead_in[q.start()].endswith(("answering ", "in reply to "))]
    if not quotes:
        return None
    m, text = quotes[0], lead_in[quotes[0].start()]
    relay = text.lower().rfind("relaying")
    if relay >= 0 and not OWN.search(text[relay:]):
        m = next((q for q in quotes[1:] if OWN.search(lead_in[q.start()])), None)
        if m is None:
            return listed(decision_text(entry))
    if (not entry[:m.start()].endswith((": ", ":** ", ".** ", "chose ")) or entry.startswith(' / "', m.end())
            or "not a decision" in entry):
        return listed(decision_text(entry))
    effect = re.search(r"\bEffect: (.+?\.)(?=\s|$)|\bEffect: (.+)$", entry)
    head = (effect[1] or effect[2]).strip() if effect else ""
    if len(head) > 120 and ";" in head:  # a long headline keeps its first clause, as a sentence
        head = head.split(";", 1)[0].rstrip() + "."
    return listed((head[:1].upper() + head[1:] + " " if head else "") + f'You: "{m[1]}"')


def picture(shipped):
    """The first path in a Last shipped line ending in .png, or None. Web links are not paths."""
    for token in re.findall(r"[^\s`'\"()\[\]<>]+\.png(?!\w)", shipped or ""):
        if "://" not in token:
            return token
    return None


class StatusPoller:
    interval = 5

    def __init__(self, conn, structure, typist=None):
        self.conn, self.structure, self.typist = conn, structure, typist  # typist: Dylan's login, for his last look
        self.mtimes = {}  # (source, path) -> the modified time last read

    def poll(self):
        at, read_now = db.now(), {}
        with self.conn:
            for team, t in self.structure.data.get("teams", {}).items():
                for kind, read in (("status", self.status), ("decisions", self.decisions)):
                    path = t.get(kind)
                    if not isinstance(path, str) or not path:
                        continue
                    source = f"{kind}:{team}"
                    try:
                        st = os.stat(expand(path))
                        if self.mtimes.get((source, path)) != st.st_mtime_ns:
                            with open(expand(path), encoding="utf-8", errors="replace") as f:
                                lines = f.read().splitlines()
                            base = self.conn.execute(
                                "SELECT 1 FROM first_reads WHERE team = ? AND kind = ? AND path = ?",
                                (team, kind, path)).fetchone() is None
                            read(team, path, lines, st.st_mtime, at, base)
                            self.conn.execute("INSERT OR IGNORE INTO first_reads (team, kind, path, at) VALUES (?, ?, ?, ?)",
                                              (team, kind, path, at))
                            read_now[(source, path)] = st.st_mtime_ns
                    except OSError as e:
                        self.mtimes.pop((source, path), None)
                        db.set_health(self.conn, source, at, "failing", f"{path}: {e.strerror or e}")
                        continue
                    db.set_health(self.conn, source, at)
        self.mtimes |= read_now  # only once committed: a failed write reads the file again on the next poll

    # base: this is the first read of this file (team, kind and path): what it holds now is the baseline, never new.
    def status(self, team, path, lines, modified, at, base):
        found = three(lines)[0]
        self.conn.execute(
            """INSERT INTO team_status (team, path, modified, working, shipped, waiting, read_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (team) DO UPDATE SET path = excluded.path, modified = excluded.modified,
                   working = excluded.working, shipped = excluded.shipped, waiting = excluded.waiting,
                   read_at = excluded.read_at""",
            (team, path, datetime.fromtimestamp(modified, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
             found.get("working"), found.get("shipped"), found.get("waiting"), at))
        for kind in ("working", "shipped", "waiting"):
            # A line that appears, changes or is cleared (value NULL) is a change; no row yet counts as cleared.
            value = found.get(kind)
            last = self.conn.execute("SELECT value FROM observations WHERE team = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                                     (team, kind)).fetchone()
            if (last[0] if last else None) != value:
                # A line has no written time of its own, so a value already there at the first read is baseline.
                self.conn.execute("INSERT INTO observations (team, kind, value, first_seen) VALUES (?, ?, ?, ?)",
                                  (team, kind, value, None if base else at))

    def decisions(self, team, path, lines, modified, at, base):
        skipped, by_time = 0, {}
        for entry in entries(lines):
            w = written(entry, modified)
            if w is None:
                skipped += 1
                continue
            by_time.setdefault(w, []).append(entry)
        if self.conn.execute("DELETE FROM decision_rekey WHERE team = ?", (team,)).rowcount:
            self.rekey(team, by_time)
        # A decision indexed late and written before Dylan's last look arrives quietly: first_seen is its written time.
        look = self.conn.execute("SELECT at FROM last_look WHERE login = ?", (self.typist,)).fetchone() \
            if self.typist else None
        for w, group in by_time.items():
            # A decision is its written time and its order among the file's entries with that time: an edit to its
            # words updates its row, which keeps its first_seen, and is never new.
            for seq, entry in enumerate(group):
                first = w if base or (look and w < look[0]) else at
                self.conn.execute(
                    """INSERT OR IGNORE INTO observations (team, kind, value, first_seen, written_at, seq)
                       VALUES (?, 'decision', ?, ?, ?, ?)""", (team, entry, first, w, seq))
                self.conn.execute(
                    """UPDATE observations SET value = ? WHERE kind = 'decision' AND team = ? AND written_at = ?
                       AND seq = ? AND value IS NOT ?""", (entry, team, w, seq, entry))
        db.set_health(self.conn, f"decisions:{team}", at)
        self.conn.execute("UPDATE source_health SET skipped = ? WHERE source = ?", (skipped, f"decisions:{team}"))

    def rekey(self, team, by_time):
        """Once per team after migration 10: each stored decision row moves to the seq its exact text has among the
        file's entries now readable at its written time, keeping its id and first_seen. A row whose text no entry holds
        keeps its seq while that slot is free; if an entry's row needs it, the row moves to the first seq past the
        entries at that time, its text and first_seen unchanged, and is logged. After this, position alone counts."""
        rows = self.conn.execute("SELECT id, written_at, seq, value FROM observations WHERE kind = 'decision' "
                                 "AND team = ?", (team,)).fetchall()
        new = {}  # id -> seq
        for w in {r["written_at"] for r in rows}:
            here, slots = [r for r in rows if r["written_at"] == w], dict(enumerate(by_time.get(w, [])))
            for r in here:  # exact text first, one entry to one row, in file order
                hit = next((s for s, e in slots.items() if e == r["value"]), None)
                if hit is not None:
                    new[r["id"]] = hit
                    del slots[hit]
            taken, extra = {new[r["id"]] for r in here if r["id"] in new}, len(by_time.get(w, []))
            for r in here:
                if r["id"] in new:
                    continue
                if r["seq"] in taken:
                    while extra in taken:
                        extra += 1
                    print(f"decisions: {team} row {r['id']} at {w}: no entry has its text and another entry's row "
                          f"takes seq {r['seq']}; moved to seq {extra}", flush=True)
                new[r["id"]] = r["seq"] if r["seq"] not in taken else extra
                taken.add(new[r["id"]])
        moved = [(r["id"], new[r["id"]]) for r in rows if new[r["id"]] != r["seq"]]
        # Two steps, so no two rows share a (team, written_at, seq) key on the way.
        self.conn.executemany("UPDATE observations SET seq = ? WHERE id = ?", [(-1 - i, i) for i, _ in moved])
        self.conn.executemany("UPDATE observations SET seq = ? WHERE id = ?", [(s, i) for i, s in moved])
