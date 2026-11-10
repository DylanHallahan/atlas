"""Work items: one small file each in a team's items folder (structure.toml `items`), written by bin/work and read here.
The id of an item is <team>:<slug>, the file name without .md. This module parses the flat frontmatter, indexes the
folders every 30 s (ItemsPoller: tables items, item_lines, item_links, all rebuilt from the files), and serves
/api/items and /api/item. Atlas never writes an item file; integrity faults are shown when read, never fixed."""
import json
import os
import re
import stat
import tomllib
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
from textwrap import shorten

from . import db, reports
from .structure import expand
from .files import open_below, open_file, open_root, read_fd

ET = ZoneInfo("America/New_York")
MAX = 1 << 20  # a larger file is skipped and counted
STATES = ("backlog", "todo", "doing", "waiting", "proving", "done", "dropped")  # backlog is read, never written
CLOSED = ("done", "dropped")
# Stale when read: an open item untouched this long. Waiting on Dylan with a default is never stale.
LIMITS = {"doing": timedelta(hours=4), "todo": timedelta(hours=24), "waiting": timedelta(hours=24),
          "proving": timedelta(days=3), "backlog": timedelta(days=3)}
KEY = re.compile(r"([A-Za-z_][\w-]*):[ \t]*(.*)$")
ID = re.compile(r"[a-z0-9][a-z0-9-]*:[\w.-]+")
ITEM_LINE = re.compile(r"[ \t]*Item:[ \t]*`?([a-z0-9][a-z0-9-]*:[\w.-]+)`?[ \t]*$")
DATED = re.compile(r"- (?P<at>\d{4}-\d{2}-\d{2}(?: \d{1,2}:\d{2}(?: [AP]M ET| UTC)?(?: \(\d{1,2}:\d{2} UTC\))?)?)"
                   r"[ \t]*[,:][ \t]*(?P<rest>.*)$")
AGENT = re.compile(r"([\w-]+): (.*)$")
PR = re.compile(r"https://github\.com/[\w.-]+/([\w.-]+)/pull/(\d+)")


def stamp(now=None):
    """The clock as the box writes it: `2026-11-01 9:53 AM ET (14:53 UTC)`, and the same moment as an ISO time in ET."""
    now = (now or datetime.now(UTC)).astimezone(ET)
    u = now.astimezone(UTC)
    return f"{now:%Y-%m-%d} {now:%-I:%M %p} ET ({u:%H:%M} UTC)", now.isoformat(timespec="seconds")


def scalar(v):
    v = v.strip()
    if v.startswith('"'):
        value = json.loads(v)
        if not isinstance(value, str):
            raise ValueError("expected a quoted string")
        return value
    if v.startswith("'"):
        if not re.fullmatch(r"'(?:[^']|'')*'", v):
            raise ValueError("invalid single-quoted string")
        return v[1:-1].replace("''", "'")
    if v.startswith("[[") and re.fullmatch(r"\[\[[^\[\]]+\]\]", v):
        return v
    if v.startswith(("[", "{")) or v.endswith(("]", "}")):
        raise ValueError("expected a scalar or a simple list")
    return v


def split_flow(v):
    """The items of an inline list `[a, "b, c"]`."""
    if not v.endswith("]"):
        raise ValueError("list is not closed")
    out, cur, quote, escaped = [], "", None, False
    for c in v[1:-1]:
        if quote:
            if escaped:
                escaped = False
            elif c == "\\" and quote == '"':
                escaped = True
            elif c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == ",":
            if not cur.strip():
                raise ValueError("empty list entry")
            out.append(cur)
            cur = ""
            continue
        cur += c
    if quote:
        raise ValueError("quoted list entry is not closed")
    return [scalar(x) for x in out + [cur] if x.strip()]


def parse(text):
    """{fm: [(key, raw lines)], vals: {key: str or list}, body: [lines], error: str or None}. The frontmatter is flat:
    key, colon, a scalar (plain or quoted) or a list (inline or with `- ` lines). Anything else is an error, and the
    file is shown as unreadable, never guessed."""
    lines = text.splitlines()
    out = {"fm": [], "vals": {}, "body": [], "error": None}
    if not lines or lines[0].strip() != "---":
        out["error"] = "no frontmatter"
        return out
    end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
    if end is None:
        out["error"] = "frontmatter is not closed"
        return out
    out["body"] = lines[end + 1:]
    for n, l in enumerate(lines[1:end], 2):
        if not l.strip() or l.lstrip().startswith("#"):
            continue
        m = KEY.match(l)
        if m:
            if m[1] in out["vals"]:
                out["error"] = f"line {n}: {m[1]} is written twice"
                return out
            v = m[2].strip()
            out["fm"].append((m[1], [l]))
            try:
                out["vals"][m[1]] = split_flow(v) if v.startswith("[") and not v.startswith("[[") else scalar(v)
            except ValueError as e:
                out["error"] = f"line {n}: {e}"
                return out
        elif out["fm"] and l[:1] in " \t-":
            key = out["fm"][-1][0]
            out["fm"][-1][1].append(l)
            item = l.strip()
            if not item.startswith("- ") and item != "-":
                out["error"] = f"line {n}: not a list item"
                return out
            if out["vals"][key] == "":
                out["vals"][key] = []
            if not isinstance(out["vals"][key], list):
                out["error"] = f"line {n}: {key} has a value and list items"
                return out
            try:
                out["vals"][key].append(scalar(item[1:]))
            except ValueError as e:
                out["error"] = f"line {n}: {e}"
                return out
        else:
            out["error"] = f"line {n}: not `key: value`"
            return out
    for key in ("type", "owner", "status", "next", "waiting_on", "default", "updated", "parent", "closed_note",
                "theirs", "due", "area", "proof", "stands"):
        if key in out["vals"] and not isinstance(out["vals"][key], str):
            out["error"] = f"{key} must be a scalar"
            break
    return out


def moment(v):
    """An `updated` value as a UTC datetime: a plain date counts from midnight ET; None when unreadable."""
    if not isinstance(v, str) or not v:
        return None
    try:
        d = datetime.fromisoformat(v.strip())
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=ET)).astimezone(UTC)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def text_of(v):
    return v if isinstance(v, str) else ""


def parent_ref(parent):
    """A wiki parent is a text reference, never an item edge or a URL."""
    return parent[2:-2] if parent.startswith("[[") and parent.endswith("]]") else None


def parent_id(parent):
    return "" if parent_ref(parent) is not None else parent


def lines_of(body):
    """The dated lines of the body: (at as written, agent or None, text)."""
    out = []
    for l in body:
        m = DATED.match(l)
        if m:
            a = AGENT.match(m["rest"])
            out.append((m["at"], a[1] if a else None, a[2] if a else m["rest"]))
    return out


def title_of(body, slug):
    for l in body:
        if l.startswith("# "):
            return l[2:].strip()
    title = next((l.strip() for l in body if l.strip() and not DATED.match(l)), slug)
    return shorten(title, width=100, placeholder="…", break_on_hyphens=False) if len(title) > 100 else title


def row_of(team, slug, text):
    """(items row without path, mtime and size; its item_lines) from one file's text."""
    p = parse(text)
    v, error = p["vals"], p["error"]
    status = text_of(v.get("status")).strip().lower()
    at = moment(text_of(v.get("updated")))
    if not error and status not in STATES:
        error = f"status {status!r} is not one of {', '.join(STATES)}"
    elif not error and at is None:
        error = f"updated {text_of(v.get('updated'))!r} is not a date or time"
    parent = text_of(v.get("parent")).strip()
    if parent and ":" not in parent and re.fullmatch(r"[\w.-]+", parent):
        parent = f"{team}:{parent}"
    waiting = text_of(v.get("waiting_on")).strip()
    row = {"id": f"{team}:{slug}", "team": team, "title": title_of(p["body"], slug), "owner": text_of(v.get("owner")).strip(),
           "status": status or None, "next": text_of(v.get("next")), "waiting_on": waiting,
           "waiting_on_item": waiting if ID.fullmatch(waiting) else None,
           "waiting_default": text_of(v.get("default")).strip(), "parent": parent, "updated": iso(at),
           "closed_note": text_of(v.get("closed_note")), "proof": text_of(v.get("proof")),
           "stands": text_of(v.get("stands")), "theirs": int(text_of(v.get("theirs")).lower() == "true"),
           "refs": json.dumps(v["links"] if isinstance(v.get("links"), list) else [v["links"]] if v.get("links") else []),
           "parse_error": error}
    return row, lines_of(p["body"])


def entries(root):
    """(name, lstat) of each regular *.md directly in the items folder root, and how many entries could not be statted.
    The folder is opened by descriptors from /, so a symlink at any component raises OSError; hidden names and *.key
    are never opened."""
    fd, found, failed = open_root(root), [], 0
    try:
        with os.scandir(fd) as it:
            for e in it:
                if reports.skipped(e.name) or not e.name.endswith(".md"):
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    failed += 1
                    continue
                if stat.S_ISREG(st.st_mode):
                    found.append((e.name, st))
    finally:
        os.close(fd)
    return found, failed


def load(root, name, dir_fd=None):
    """(text, fstat) of one item file, read by descriptor with no symlink followed; OSError on a symlink or a
    non-regular file, ValueError when over the size cap."""
    if reports.skipped(name) or "/" in name:
        raise OSError("excluded item name")
    fd = os.dup(dir_fd) if dir_fd is not None else open_root(root)
    try:
        f = open_file(name, fd)
    finally:
        os.close(fd)
    st = os.fstat(f)
    if st.st_size > MAX:
        os.close(f)
        raise ValueError("over the size cap")
    return read_fd(f, MAX + 1).decode("utf-8", errors="replace"), st


def team_roots(data):
    """{team key: its items folder as an absolute path, ~ expanded} for each team of structure.toml with `items`."""
    return {k: os.path.normpath(expand(t["items"])) for k, t in data.get("teams", {}).items()
            if isinstance(t.get("items"), str) and t["items"]}


def read_structure(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def waits_on_dylan(waiting_on):
    return (waiting_on or "").partition(":")[0].strip().casefold() == "dylan"


def stale(status, updated, waiting_on, default, now):
    """Open and untouched past its status's limit. updated is an ISO UTC string or None."""
    limit, at = LIMITS.get(status), moment(updated)
    if limit is None or at is None:
        return False
    if status == "waiting" and waits_on_dylan(waiting_on) and default:
        return False
    return now - at > limit


def untouched(status, updated, waiting_on, now):
    """The board's cross-team signal, independent of the CLI's tighter stale limits."""
    at = moment(updated)
    return bool(status in LIMITS and at and not waits_on_dylan(waiting_on)
                and now - at > timedelta(days=3))


def line_time(at):
    """A dated trail line as UTC, retaining its original `at` alongside it."""
    for fmt, zone in (("%Y-%m-%d %I:%M %p ET", ET), ("%Y-%m-%d %H:%M UTC", UTC),
                      ("%Y-%m-%d %H:%M", ET), ("%Y-%m-%d", ET)):
        try:
            return iso(datetime.strptime(at.split(" (")[0], fmt).replace(tzinfo=zone).astimezone(UTC))
        except ValueError:
            pass
    return None


def destinations(conn, review=None):
    """Reports which have an Atlas page, with the ask's own words."""
    pages = {r["path"]: {"team": r["team"], "name": r["name"], "ask": bool(r["has_ask"]),
                        "labels": json.loads(r["ask_labels"] or "{}")}
            for r in conn.execute("SELECT path, team, name, has_ask, ask_labels FROM reports")}
    for repo, data in (review or {}).get("repos", {}).items():
        for change in data["changes"]:
            if change["n"] is not None:
                pages[f"{repo}#{change['n']}"] = {"href": f"/pr/{quote(repo, safe='')}/{change['n']}"}
    return pages


def destination(ref, pages):
    if m := PR.search(ref):
        return pages.get(f"{m[1]}#{m[2]}", {}).get("href")
    if m := re.fullmatch(r"([\w.-]+)#(\d+)", ref):
        return pages.get(ref, {}).get("href")
    if p := pages.get(os.path.normpath(expand(ref))):
        return f"/{'ask' if p['ask'] else 'change'}/{quote(p['team'], safe='')}/{quote(p['name'], safe='/')}"
    return None


def display_ref(ref, team):
    """A reference as written, with local team prefixes removed and PR URLs named by number."""
    text = PR.sub(lambda m: f"PR {m[2]}", ref)
    if m := re.fullmatch(r"[\w.-]+#(\d+)", text):
        return f"PR {m[1]}"
    if text.startswith(("/", "~/")):
        text = os.path.normpath(expand(text))
    roots = list((team or {}).get("folders", []))
    roots += [os.path.dirname(p) for key in ("reports", "items") if (p := (team or {}).get(key))]
    for root in roots:
        text = text.replace(os.path.normpath(expand(root)).rstrip("/") + "/", "")
    return text


def board_data(row, links, pages, now, team=None):
    ask = next((pages[l["ref"]] | {"href": destination(l["ref"], pages)} for l in reversed(links)
                if l["kind"] == "ask" and l["ref"] in pages), None)
    closed = row["status"] in CLOSED and moment(row["updated"])
    return {"untouched": untouched(row["status"], row["updated"], row["waiting_on"], now),
            "waiting_on_you": row["status"] not in CLOSED and waits_on_dylan(row["waiting_on"]),
            "ask": {"question": ask["labels"].get("Question", ""), "default": ask["labels"].get("Default", ""),
                    "href": ask["href"]} if ask else None,
            "closed_this_week": bool(closed and now - timedelta(days=7) <= closed <= now),
            "closure": [{"text": ref, "label": display_ref(ref, team), "href": destination(ref, pages)} for ref in dict.fromkeys(
                [row["closed_note"]] if row["closed_note"] else
                [l["ref"] for l in links if l["kind"] in ("report", "pr")] + json.loads(row["refs"]))]}


class ItemsPoller:
    interval = 30

    def __init__(self, conn, structure):
        self.conn, self.structure = conn, structure
        self.loaded = set()  # reread once at startup so derived titles follow the current reader
        self.scanned = {}  # (team, source path) -> ((mtime_ns, size), contributed links)

    def poll(self):
        if self.structure.mtime is None:  # not loaded yet: an empty structure purges nothing
            return
        at = db.now()
        configured = self.structure.data.get("teams", {})
        teams = {k: t for k, t in configured.items()
                 if isinstance(t.get("items"), str) and t["items"]}
        sources = {k: t for k, t in configured.items() if isinstance(t.get("reports"), str) and t["reports"]}
        with self.conn:
            for table, active in (("items", teams), ("item_links", sources)):
                self.conn.execute(f"DELETE FROM {table} WHERE team NOT IN (SELECT value FROM json_each(?))",
                                  (json.dumps(list(active)),))
            self.conn.execute("DELETE FROM item_lines WHERE item NOT IN (SELECT id FROM items)")
            for team, t in teams.items():
                self.team(team, t, at)
            for team, t in sources.items():
                errors = self.links(team, t)
                db.set_health(self.conn, f"item-links:{team}", at, "degraded" if errors else "ok",
                              errors[0] if errors else None, len(errors))

    def team(self, team, t, at):
        source, root, errors, big = f"items:{team}", team_roots({"teams": {team: t}})[team], [], 0
        absent = None
        try:
            found, failed = entries(root)
        except FileNotFoundError:
            found, failed = [], 0
            absent = f"absent: {t['items']}"
        except OSError as e:  # an unreadable root or a symlink at some component: its rows stay as they were
            db.set_health(self.conn, source, at, "failing", f"{t['items']}: {e.strerror or e}")
            return
        if failed:
            errors.append(f"{failed} entries below {t['items']} could not be statted")
        stored = {r[0]: (r[1], r[2]) for r in self.conn.execute("SELECT id, mtime_ns, size FROM items WHERE team = ?", (team,))}
        keep = set()
        for name, st in found:
            id_ = f"{team}:{name.removesuffix('.md')}"
            if st.st_size > MAX:
                big += 1
                continue
            keep.add(id_)
            if id_ in self.loaded and stored.get(id_) == (st.st_mtime_ns, st.st_size):
                continue
            try:
                text, st = load(root, name)
            except ValueError:
                big += 1
                keep.discard(id_)
                continue
            except OSError as e:  # a file that fails to read keeps its last row
                errors.append(f"{name}: {e.strerror or e}")
                continue
            row, lines = row_of(team, name.removesuffix(".md"), text)
            self.loaded.add(id_)
            row |= {"path": os.path.join(root, name), "mtime_ns": st.st_mtime_ns, "size": st.st_size}
            self.conn.execute(f"INSERT OR REPLACE INTO items ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                              tuple(row.values()))
            self.conn.execute("DELETE FROM item_lines WHERE item = ?", (id_,))
            self.conn.executemany("INSERT INTO item_lines (item, seq, at, agent, text) VALUES (?, ?, ?, ?, ?)",
                                  [(id_, i, *l) for i, l in enumerate(lines)])
        if not errors:  # only a complete read removes rows
            self.conn.execute("DELETE FROM items WHERE team = ? AND id NOT IN (SELECT value FROM json_each(?))",
                              (team, json.dumps(sorted(keep))))
            self.conn.execute("DELETE FROM item_lines WHERE item NOT IN (SELECT id FROM items)")
            db.set_health(self.conn, source, at, error=absent)
        else:
            more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
            db.set_health(self.conn, source, at, "degraded", errors[0] + more, len(errors))
        self.conn.execute("UPDATE source_health SET skipped = ? WHERE source = ?", (big, source))

    def links(self, team, t):
        """Aggregate all report/brief contributions, caching each source independently. Recompute the aggregate from
        cached contributions so changing one report cannot erase a PR still cited by another."""
        among, errors, seen, contributions = reports.roots(t), [], set(), []
        sources = []
        if isinstance(t.get("reports"), str) and t["reports"]:
            folder = among[0]
            try:
                found, failed = reports.walk(folder)
                sources += [("report", folder, rel, st) for rel, st in found]
                if failed:
                    errors.append(f"{failed} entries below {folder} could not be listed")
            except OSError as e:
                return [f"{t['reports']}: {e.strerror or e}"]
            briefs = os.path.join(os.path.dirname(folder), "briefs")
            if (hit := reports.inside(briefs, among)) and not any(map(reports.skipped, hit[1].split("/"))):
                try:
                    found, failed = reports.walk(briefs)
                    sources += [("brief", briefs, rel, st) for rel, st in found]
                    if failed:
                        errors.append(f"{failed} entries below {briefs} could not be listed")
                except FileNotFoundError:
                    pass
                except OSError as e:
                    errors.append(f"{briefs}: {e.strerror or e}")
        for kind, folder, rel, st in sorted(sources, key=lambda s: os.path.join(s[1], s[2])):
            path = os.path.join(folder, rel)
            if st.st_size > MAX:
                continue
            seen.add(path)
            cached = self.scanned.get((team, path))
            if cached and cached[0] == (st.st_mtime_ns, st.st_size):
                contributions.extend(cached[1])
                continue
            try:
                data = read_fd(open_below(folder, rel), MAX + 1)
            except OSError as e:
                errors.append(f"{path}: {e.strerror or e}")
                continue
            if len(data) > MAX:
                seen.discard(path)
                continue
            text = data[:MAX].decode("utf-8", errors="replace")
            rows = []
            if item := item_of(text):
                p = reports.parse(text, os.path.basename(path), st.st_mtime)
                found = [("ask" if kind == "report" and p["has_ask"] else kind, path)]
                if (m := PR.search(text)):
                    found.append(("pr", f"{m[1]}#{m[2]}"))
                rows = [(item, k, ref, p["written_at"], team, path) for k, ref in found]
            self.scanned[(team, path)] = ((st.st_mtime_ns, st.st_size), rows)
            contributions.extend(rows)
        if not errors:
            self.conn.execute("DELETE FROM item_links WHERE team = ?", (team,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO item_links (item, kind, ref, at, team, source_path) VALUES (?, ?, ?, ?, ?, ?)",
                contributions)
            for key in list(self.scanned):
                if key[0] == team and key[1] not in seen:
                    del self.scanned[key]
        return errors


def item_of(text):
    """The id on the first `Item: <id>` line of the preamble: the lines before the first ## heading, outside code
    fences, within the first 30 lines."""
    lines = text.splitlines()[:30]
    fence = reports.fenced(lines)
    for l, f in zip(lines, fence):
        if not f and l.startswith("## "):
            return None
        if not f and (m := ITEM_LINE.fullmatch(l)):
            return m[1]
    return None


def notes(row, by_id):
    """The integrity faults of one item, read now: shown on the item and never fixed."""
    out = []
    if row["parse_error"]:
        out.append(f"the file cannot be parsed: {row['parse_error']}")
    if parent := parent_id(row["parent"]):
        up = by_id.get(parent)
        if up is None:
            out.append(f"parent names {row['parent']}, which Atlas cannot find")
        elif up is not None and parent_id(up["parent"]):
            out.append(f"second level of nesting: parent {row['parent']} is itself a child of {up['parent']}")
    if row["waiting_on_item"] and row["waiting_on_item"] not in by_id:
        out.append(f"waiting_on names {row['waiting_on_item']}, which Atlas cannot find")
    return out


def view(row, by_id, kids, now):
    r = {k: row[k] for k in ("id", "team", "title", "owner", "status", "next", "waiting_on", "waiting_default", "parent",
                             "updated", "closed_note", "proof", "stands", "parse_error")}
    r["parent"], r["ref"] = parent_id(row["parent"]), parent_ref(row["parent"])
    c = kids.get(row["id"], [])
    r |= {"theirs": bool(row["theirs"]), "stale": stale(row["status"], row["updated"], row["waiting_on"],
                                                         row["waiting_default"], now),
          "children": {"total": len(c), "done": sum(k["status"] == "done" for k in c),
                       "dropped": sum(k["status"] == "dropped" for k in c),
                       "open": sum(k["status"] not in CLOSED for k in c)},
          "problems": notes(row, by_id)}
    r["waiting_item"] = row["waiting_on_item"] if row["waiting_on_item"] in by_id else None
    return r


def everything(conn):
    rows = [dict(r) for r in conn.execute("SELECT * FROM items ORDER BY team, id")]
    by_id, kids = {r["id"]: r for r in rows}, {}
    for r in rows:
        if parent_id(r["parent"]):
            kids.setdefault(r["parent"], []).append(r)
    return rows, by_id, kids


def listing(conn, review=None, teams=None):
    """GET /api/items: every item, with its stale flag, its children's roll-up and its integrity notes."""
    with db.read(conn):
        rows, by_id, kids = everything(conn)
        pages = destinations(conn, review)
        links = {}
        for l in conn.execute("SELECT * FROM item_links ORDER BY at, kind, ref"):
            links.setdefault(l["item"], []).append(dict(l))
    now = datetime.now(UTC)
    return {"as_of": iso(now), "items": [view(r, by_id, kids, now) | board_data(r, links.get(r["id"], []), pages, now,
                                                                          (teams or {}).get(r["team"]))
                                         for r in rows]}


def one(conn, id_, review=None, teams=None):
    """GET /api/item?id=: one item in full, or None."""
    with db.read(conn):
        rows, by_id, kids = everything(conn)
        row = by_id.get(id_)
        if row is None:
            return None
        lines = [dict(r) for r in conn.execute("SELECT at, agent, text FROM item_lines WHERE item = ? ORDER BY seq", (id_,))]
        links = [dict(r) for r in conn.execute(
            "SELECT kind, ref, at, team, source_path FROM item_links WHERE item = ? ORDER BY at, kind, ref", (id_,))]
        pages = destinations(conn, review)
    now = datetime.now(UTC)
    team = (teams or {}).get(row["team"])
    trail = sorted([l | {"at_iso": line_time(l["at"]), "kind": "note"} for l in lines] +
                        [l | {"at_iso": iso(moment(l["at"])), "text": l["ref"],
                              "href": destination(l["ref"], pages)} for l in links] +
                        [{"kind": "link", "at_iso": None, "text": ref, "href": destination(ref, pages)}
                         for ref in json.loads(row["refs"]) if ref not in {l["ref"] for l in links}],
                        key=lambda l: l["at_iso"] or "9999")
    for entry in trail:
        entry["label"] = display_ref(entry["text"], team)
    return view(row, by_id, kids, now) | board_data(row, links, pages, now, team) | {
        "path": row["path"], "path_label": display_ref(row["path"], team),
        "refs": json.loads(row["refs"]), "lines": lines, "links": links, "trail": trail,
        "child_items": [view(k, by_id, kids, now) for k in kids.get(id_, [])]}
