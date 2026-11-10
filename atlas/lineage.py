"""The lineage and schema change of a change that touches a dbt project, for the Story page: ref() and source() from the
model SQL and the columns from the model .yml files, read from git objects (git.tree, git.blobs) at the merge base and at
the head. Read when a Story page asks, never in the refs pass; cached by (clone path, commit id). No checkout, fetch,
dbt run or warehouse read. What does not read cleanly says so and is never guessed."""
import re

from . import git, review

PROJECTS = review.Memo(16)
REF = re.compile(r"""\bref\(\s*(['"])(\w+)\1\s*\)""")
SRC = re.compile(r"""\bsource\(\s*(['"])(\w+)\1\s*,\s*(['"])(\w+)\3\s*\)""")
MODEL_FILE = 2 << 20  # a model file larger than this is not read
NAME = re.compile(r"[\w.-]+")


class Unreadable(Exception):
    pass


def strip_comments(sql):
    """The SQL without its {# ... #} comments, in one forward scan (each search starts where the last ended, so the
    work is linear). An opener with no closer is Unreadable: where the comment ends is not known."""
    out, at = [], 0
    while True:
        i = sql.find("{#", at)
        if i < 0:
            out.append(sql[at:])
            return "".join(out)
        j = sql.find("#}", i + 2)
        if j < 0:
            raise Unreadable("comment")
        out.append(sql[at:i])
        at = j + 2


def bare(v):
    """A YAML value without its trailing ` #` comment and the spaces around it, by string search (no regex)."""
    i = 0 if v.startswith("#") else v.find(" #")
    return (v[:i] if i >= 0 else v).strip()


def scalar(v):
    """One plain or quoted YAML scalar, else Unreadable (a flow value, an anchor, a block scalar, an empty value)."""
    v = (v or "").strip()
    if v[:1] in "'\"":
        # The flow reader's quote rules: one pair around the whole value, no quote of the same kind inside (a doubled
        # single quote included) and no escape; anything else is not read, never returned as raw bytes.
        if len(v) < 2 or v[-1] != v[0] or v[0] in v[1:-1] or "\\" in v:
            raise Unreadable(v)
        return v[1:-1]
    v = bare(v)
    if not v or v[0] in "[{&*!|>%@`" or ": " in v:
        raise Unreadable(v)
    return v


def flow(text):
    """One flow value on one line, as YAML writes it: `[a, b]`, `{k: v, k2: v2}`, nested to any depth, with plain or
    quoted scalars (a quoted one holds no escape). The whole text must be consumed, bar a trailing comment. Anything else
    raises Unreadable. Returns lists, dicts and strings."""
    at = 0

    def skip():
        nonlocal at
        while at < len(text) and text[at] == " ":
            at += 1

    def item(stop):
        nonlocal at
        skip()
        c = text[at:at + 1]
        if c == "[":
            at += 1
            out = []
            while True:
                skip()
                if text[at:at + 1] == "]":
                    at += 1
                    return out
                out.append(item(",]"))
                skip()
                if text[at:at + 1] == ",":
                    at += 1
                elif text[at:at + 1] != "]":
                    raise Unreadable("flow")
        if c == "{":
            at += 1
            out = {}
            while True:
                skip()
                if text[at:at + 1] == "}":
                    at += 1
                    return out
                key = item(":")
                skip()
                if not isinstance(key, str) or text[at:at + 1] != ":" or key in out:
                    raise Unreadable("flow")
                at += 1
                out[key] = item(",}")
                skip()
                if text[at:at + 1] == ",":
                    at += 1
                elif text[at:at + 1] != "}":
                    raise Unreadable("flow")
        if c in "'\"":
            end = text.find(c, at + 1)
            if end < 0:
                raise Unreadable("flow")
            val = text[at + 1:end]
            if "\\" in val or (c == "'" and text[end + 1:end + 2] == "'"):
                raise Unreadable("flow")
            at = end + 1
            return val
        start = at
        while at < len(text) and text[at] not in ",]}":
            ch, nxt = text[at], text[at + 1:at + 2]
            if ch == ":" and stop == ":" and nxt in (" ", ""):
                break  # the end of a key
            if ch in "[{" or (ch == ":" and nxt == " ") or (ch == " " and nxt == "#"):
                raise Unreadable("flow")
            at += 1
        val = text[start:at].strip()
        if not val or val[0] in "&*!|>%@`#":
            raise Unreadable("flow")
        return val

    value = item("")
    rest = text[at:].strip()
    if rest and not rest.startswith("#"):
        raise Unreadable("flow")
    return value


def key_line(content):
    m = re.fullmatch(r"([A-Za-z_][\w-]*):(?:\s+(.*))?", content.rstrip())
    if not m:
        raise Unreadable(content)
    return m[1], m[2]


def read_yml(text):
    """{model: [(column, data_type or None)] or None} from the `models:` list of one model .yml: `- name:`, then
    `columns:`, then `- name:` and `data_type:`, at the indents the file itself sets. Anything deeper is another key's
    and is skipped. None is a model with no `columns:`. A file with no `models:` gives {}. Raises Unreadable on a tab,
    a flow list, an anchor, a repeat, a missing name or an indent that does not line up."""
    lines = []
    for raw in text.split("\n"):
        if "\t" in raw:
            raise Unreadable("tab")
        c = raw.lstrip(" ")
        if c.strip() and not c.startswith("#"):
            lines.append((len(raw) - len(c), c.rstrip()))
    top = [i for i, (n, c) in enumerate(lines) if n == 0 and c.startswith("models:")]
    if len(top) > 1 or (top and not re.fullmatch(r"models:\s*(#.*)?", lines[top[0]][1])):  # one header, read once
        raise Unreadable("models")
    if not top:
        return {}
    end = next((j for j in range(top[0] + 1, len(lines)) if lines[j][0] == 0 and not lines[j][1].startswith("- ")), len(lines))
    block, out = lines[top[0] + 1:end], {}
    if not block or not block[0][1].startswith("- "):
        raise Unreadable("models list")
    ia = block[0][0]
    starts = [i for i, (n, c) in enumerate(block) if n == ia and c.startswith("- ")]
    for k, s in enumerate(starts):
        item = block[s:(starts[k + 1] if k + 1 < len(starts) else len(block))]
        first = item[0][1][1:]
        ka = ia + 1 + (len(first) - len(first.lstrip(" ")))
        keys, i = {}, 0
        rows = [(ka, first.lstrip(" "))] + item[1:]
        while i < len(rows):
            n, c = rows[i]
            if n != ka:
                raise Unreadable("indent")
            key, val = key_line(c)
            if key in keys:
                raise Unreadable("repeat")
            j = i + 1
            while j < len(rows) and (rows[j][0] > ka or (rows[j][0] == ka and rows[j][1].startswith("- "))):
                j += 1
            keys[key] = (val, rows[i + 1:j])
            i = j
        if "name" not in keys or keys["name"][1]:
            raise Unreadable("name")
        name = scalar(keys["name"][0])
        if not NAME.fullmatch(name) or name in out:
            raise Unreadable("name")
        out[name] = columns(keys["columns"], ka) if "columns" in keys else None
    return out


def flow_columns(val):
    """The `columns: [...]` of one line: a list of names, or of maps with a `name` and maybe a `data_type`."""
    cols, seen = [], set()
    items = flow(val)
    if not isinstance(items, list):
        raise Unreadable("columns")
    for it in items:
        name = it if isinstance(it, str) else it.get("name") if isinstance(it, dict) else None
        dtype = it.get("data_type") if isinstance(it, dict) else None
        if not isinstance(name, str) or (dtype is not None and not isinstance(dtype, str)) or name in seen:
            raise Unreadable("column name")
        seen.add(name)
        cols.append((name, dtype))
    return cols


def columns(entry, ka):
    val, rows = entry
    if val is not None:
        if val.lstrip().startswith("[") and not rows:
            return flow_columns(val)
        raise Unreadable("columns")
    if not rows or not rows[0][1].startswith("- ") or rows[0][0] < ka:
        raise Unreadable("columns list")
    ic = rows[0][0]
    starts = [i for i, (n, c) in enumerate(rows) if n == ic and c.startswith("- ")]
    if any(n < ic for n, _ in rows):
        raise Unreadable("indent")
    cols, seen = [], set()
    for k, s in enumerate(starts):
        item = rows[s:(starts[k + 1] if k + 1 < len(starts) else len(rows))]
        first = item[0][1][1:]
        kc = ic + 1 + (len(first) - len(first.lstrip(" ")))
        fields, i, flat = {}, 0, [(kc, first.lstrip(" "))] + item[1:]
        while i < len(flat):
            n, c = flat[i]
            if n != kc:
                raise Unreadable("indent")
            key, val = key_line(c)
            if key in fields:
                raise Unreadable("repeat")
            j = i + 1
            while j < len(flat) and flat[j][0] > kc:
                j += 1
            fields[key] = (val, j > i + 1)
            i = j
        if "name" not in fields or fields["name"][1]:
            raise Unreadable("column name")
        name = scalar(fields["name"][0])
        dtype = scalar(fields["data_type"][0]) if "data_type" in fields and not fields["data_type"][1] else None
        if name in seen:
            raise Unreadable("column name")
        seen.add(name)
        cols.append((name, dtype))
    return cols


def model_paths(text):
    """The `model-paths:` of dbt_project.yml (default ["models"]); Unreadable when it is there and not a plain list."""
    lines = [l for l in text.split("\n") if l.strip() and not l.lstrip().startswith("#")]
    for i, l in enumerate(lines):
        if not l.startswith("model-paths:"):
            continue
        rest = bare(l[len("model-paths:"):])
        if rest:
            if not (rest.startswith("[") and rest.endswith("]")):
                raise Unreadable("model-paths")
            items = [x for x in rest[1:-1].split(",") if x.strip()]
        else:
            items = []
            for n in lines[i + 1:]:
                t = n.lstrip()
                if not t.startswith("- "):
                    break
                items.append(bare(t[2:]))
        out = [scalar(x).strip("/") for x in items]
        if not out or any(not p or ".." in p.split("/") for p in out):
            raise Unreadable("model-paths")
        return out
    return ["models"]


def loose_names(text):
    """The model names a .yml that did not read cleanly seems to list: `- name:` lines at the shallowest indent."""
    found = [(len(l) - len(l.lstrip(" ")), m[1]) for l in text.split("\n") if (m := re.match(r"\s*-\s+name:\s*['\"]?([\w.-]+)", l))]
    if not found:
        return set()
    low = min(n for n, _ in found)
    return {x for n, x in found if n == low}


def project(path, commit):
    """The dbt project at a commit: {"dbt": bool, "models": {name: {path, refs, sources, unreadable}},
    "cols": {name: [(column, type)] or None}, "bad": set of model names whose .yml did not read, "bad_dirs": folders
    holding such a .yml, "error": text or None}. Cached by (path, commit)."""
    return PROJECTS.get((path, commit), lambda: read_project(path, commit))


def read_project(path, commit):
    listed = git.tree(path, commit)
    roots = sorted((n for _, n in listed if n.rsplit("/", 1)[-1] == "dbt_project.yml" and not git.sensitive(n)), key=lambda n: (n.count("/"), n))
    if not roots:
        return {"dbt": False}
    out = {"dbt": True, "root": roots[0], "models": {}, "cols": {}, "ymls": {}, "bad": set(), "bad_dirs": set(), "error": None}
    ids = {n: i for i, n in listed}
    cfg = git.blobs(path, listed, [ids[roots[0]]])[ids[roots[0]]]
    base = roots[0][:-len("dbt_project.yml")]
    try:
        if cfg is None:
            raise Unreadable("dbt_project.yml")
        mp = [base + p + "/" for p in model_paths(cfg.decode("utf-8", "replace"))]
    except Unreadable:
        return out | {"error": "dbt_project.yml could not be read"}
    files = [(i, n) for i, n in listed if n.endswith((".sql", ".yml", ".yaml")) and any(n.startswith(p) for p in mp) and not git.sensitive(n)]
    data = git.blobs(path, listed, [i for i, _ in files])
    for i, n in files:
        body = data[i]
        if n.endswith(".sql"):
            name = n.rsplit("/", 1)[-1][:-4]
            if name in out["models"]:
                continue  # dbt refuses two models of one name; the first by path stays
            if body is None or len(body) > MODEL_FILE:
                out["models"][name] = {"path": n, "refs": [], "sources": [], "unreadable": True}
                continue
            try:
                sql = strip_comments(body.decode("utf-8", "replace"))
            except Unreadable:
                out["models"][name] = {"path": n, "refs": [], "sources": [], "unreadable": True}
                continue
            out["models"][name] = {"path": n, "unreadable": False, "refs": sorted({m[2] for m in REF.finditer(sql)}),
                                   "sources": sorted({f"{m[2]}.{m[4]}" for m in SRC.finditer(sql)})}
        else:
            try:
                if body is None or len(body) > MODEL_FILE:
                    raise Unreadable("blob")
                listed = read_yml(body.decode("utf-8", "replace"))
                out["ymls"][n] = list(listed)
                for m, cols in listed.items():
                    out["cols"].setdefault(m, cols)
            except (Unreadable, UnicodeError, RecursionError):
                out["bad_dirs"].add(n.rsplit("/", 1)[0] if "/" in n else "")
                out["bad"] |= loose_names(body.decode("utf-8", "replace")) if body else set()
    return out


def schema_of(p, name):
    """(state, columns): "ok" with [(column, type)], "none" when the project lists no columns for this model, or
    "unreadable" when the .yml that should list them did not read."""
    if name in p["cols"] and p["cols"][name] is not None:
        return "ok", p["cols"][name]
    m = p["models"].get(name)
    if name in p["bad"] or (name not in p["cols"] and m and m["path"].rsplit("/", 1)[0] in p["bad_dirs"]):
        return "unreadable", []
    return "none", []


def lineage(c, s):
    """The Story page's data for one change (c from review, s its stats): nodes, edges and the schema change, or
    {"dbt": False}. Changed and new are measured against the merge base s["base"]."""
    out = {"repo": c["repo"], "head": c["head"], "base": s["base"]}
    if not any(f["path"].endswith((".sql", ".yml", ".yaml")) for f in s["files"]):
        return out | {"dbt": False}
    try:
        h = project(c["path"], c["head"])
        b = project(c["path"], s["base"])
    except git.GitError as e:
        return out | {"dbt": None, "error": f"Lineage could not be read: {e}"}
    if not h["dbt"] and not b["dbt"]:
        return out | {"dbt": False}
    for side, p in (("head", h), ("base", b)):
        if p.get("error"):
            return out | {"dbt": True, "error": f"Lineage could not be read at the {side}: {p['error']}"}
    hm, bm = (h["models"] if h["dbt"] else {}), (b["models"] if b["dbt"] else {})
    at_head, at_base = {m["path"]: n for n, m in hm.items()}, {m["path"]: n for n, m in bm.items()}
    touched = set()
    for f in s["files"]:
        if f["status"] in ("added", "modified", "renamed", "copied") and f["path"] in at_head:
            touched.add(at_head[f["path"]])
        if f["status"] in ("deleted", "renamed") and (f["old"] or f["path"]) in at_base:
            touched.add(at_base[f["old"] or f["path"]])
    # A model whose column list differs between base and head in a changed model .yml is changed too, though its SQL is not.
    for f in s["files"]:
        if f["path"].endswith((".yml", ".yaml")):
            listed = set(h.get("ymls", {}).get(f["path"], ())) if f["status"] != "deleted" else set()
            listed |= set(b.get("ymls", {}).get(f["old"] or f["path"], ())) if f["status"] != "added" else set()
            touched |= {m for m in listed if (m in hm or m in bm) and h.get("cols", {}).get(m) != b.get("cols", {}).get(m)}
    kind = {n: "new" if n not in bm else "removed" if n not in hm else "changed" for n in touched}
    sources = {x for p in (hm, bm) for m in p.values() for x in m["sources"]}
    nodes, edges = {}, {}

    def node(n):
        return nodes.setdefault(n, {"kind": kind.get(n) or ("source" if n in sources else "model"), "unreadable": False, "gained": [], "lost": []})

    for m in sorted(touched):
        mh, mb = hm.get(m), bm.get(m)
        info = mh or mb
        n = node(m)
        n["unreadable"] = info["unreadable"]
        now = set(mh["refs"] + mh["sources"]) if mh else set()
        was = set(mb["refs"] + mb["sources"]) if mb else set()
        if kind[m] == "changed" and not (mh["unreadable"] or mb["unreadable"]):
            n["gained"], n["lost"] = sorted(now - was), sorted(was - now)
        for r in sorted(now | was if kind[m] == "changed" else info["refs"] + info["sources"]):
            node(r)
            edges[(r, m)] = "" if r in now and (r in was or kind[m] != "changed") else "gained" if r in now else "lost"
    for name, mm in sorted(hm.items()):
        if name not in touched:
            for m in sorted(touched.intersection(mm["refs"])):
                node(name)
                edges.setdefault((m, name), "")
    rows = []
    for m in sorted(touched):
        sides = [schema_of(p, m) for p, there in ((b, m in bm), (h, m in hm)) if there]
        cb, ch = (sides[0][1] if m in bm else []), (sides[-1][1] if m in hm else [])
        states = {x for x, _ in sides}
        state = "unreadable" if "unreadable" in states else "none" if states == {"none"} else "ok"
        old, new = [x for x, _ in cb], [x for x, _ in ch]
        in_old, in_new = set(old), set(new)  # sets for membership, the lists keep the .yml's order
        ok = state == "ok"
        rows.append({"model": m, "kind": kind[m], "state": state, "added": [x for x in new if x not in in_old] if ok else [],
                     "removed": [x for x in old if x not in in_new] if ok else [], "kept": len(in_new & in_old) if ok else 0})
    bad = sorted(n for p in (h, b) if p["dbt"] for n, m in p["models"].items() if m["unreadable"])
    return out | {"dbt": True, "project": (h if h["dbt"] else b)["root"], "nodes": nodes, "edges": [[a, c2, f] for (a, c2), f in sorted(edges.items())],
                  "schema": rows, "unreadable": sorted(set(bad))}


def get(app, repo, n):
    """The lineage of the open change repo/n, or review.NotFound; a change that is not open here has none."""
    c = app.review.find(repo, n)
    s = review.stats(c)
    if "error" in s:
        return {"repo": repo, "dbt": None, "error": s["error"]}
    return {"n": review.cid(c)} | lineage(c, s)
