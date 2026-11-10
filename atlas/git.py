"""Read-only git over the repos structure.toml declares, for the code review pages. Only the subcommands in ALLOWED run,
through argv, with a timeout and an output cap, in a declared path whose real path is where it is declared. Refs from
a page never reach git: callers resolve them to object ids here, and only ids reach diff and show. There is never a
checkout, fetch, pull, gc, status, or any index or worktree write."""
import fnmatch
import os
import re
import select
import stat
import subprocess
import threading
import time

from .structure import expand

# One hardened environment and argument set for every git child. Nothing a repo's own config names may run (hooks, pager,
# fsmonitor, diff drivers, textconv, signature programs, credential helpers, transports), and a missing object is an
# error, never a lazy fetch. The environment is built from scratch, so GIT_EXTERNAL_DIFF and the like are never inherited.
CONFIG = ["core.fsmonitor=false", "core.hooksPath=/dev/null", "core.pager=cat", "gc.auto=0", "maintenance.auto=false",
          "log.showSignature=false", "gpg.program=/bin/false", "gpg.ssh.program=/bin/false", "gpg.x509.program=/bin/false",
          "protocol.allow=never", "color.ui=never", "diff.external=", "core.sshCommand=/bin/false", "credential.helper="]
BASE = ["git", "--no-optional-locks", "--no-pager", "--no-lazy-fetch", *(x for c in CONFIG for x in ("-c", c))]
ALLOWED = {"diff", "log", "show", "merge-base", "rev-parse", "for-each-ref", "worktree", "ls-tree", "cat-file"}
# Flags every call of a subcommand gets, right after its name.
FLAGS = {"diff": ["--no-ext-diff", "--no-textconv", "--no-color"], "show": ["--no-show-signature", "--no-ext-diff", "--no-textconv", "--no-color"],
         "log": ["--no-show-signature", "--no-ext-diff", "--no-textconv", "--no-color"]}
ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", ""), "LANG": "C.UTF-8",
       "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false",
       "GIT_SSH_COMMAND": "/bin/false", "GIT_ALLOW_PROTOCOL": "", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
       "GIT_PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0"}
ID = re.compile(r"[0-9a-f]{40}")
RANGE = re.compile(r"[0-9a-f]{40}\.\.[0-9a-f]{40}")
TIMEOUT, CAP = 20, 16 << 20
MAX_BLOBS = 4000
SENSITIVE = (".env", ".env.*", "*.key", "*.pem", "credentials*")


class GitError(Exception):
    pass


def sensitive(path):
    name = path.rsplit("/", 1)[-1].lower()
    return any(fnmatch.fnmatchcase(name, p) for p in SENSITIVE)


VALID = {}  # declared path as spelt -> the other paths declared for the same repo; set by declare()


def real_dir(path):
    return os.path.realpath(path)


def inside(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


WALKED = {}  # a git directory -> {every directory in it: mtime}, as of its last clean walk


def walk(root):
    """Raises GitError when a symlink is anywhere in this git directory, objects fanout, packs, refs, logs, config, info
    included. Directories are scanned by d_type without following anything. A walk that was clean stays so while every
    directory's mtime is the same (adding or swapping an entry changes its directory's mtime), so a refresh costs one
    lstat per directory."""
    seen = WALKED.get(root)
    if seen is not None:
        try:
            if all(os.lstat(d).st_mtime_ns == m for d, m in seen.items()):
                return
        except OSError:
            pass
    dirs, stack = {}, [root]
    try:
        while stack:
            d = stack.pop()
            dirs[d] = os.lstat(d).st_mtime_ns  # before the scan: a change during it shows at the next check
            with os.scandir(d) as it:
                for e in it:
                    if e.is_symlink():
                        raise GitError(f"{e.path} is a symlink in a git directory")
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
    except OSError as e:
        raise GitError(f"{root}: {e}") from e
    WALKED[root] = dirs


def check(spelt, siblings):
    """Raises GitError unless the repo's git metadata stays inside what is declared: its git dir is the .git below spelt
    (or a linked worktree whose common dir is the .git of a sibling declared for the same repo), and no symlink sits at
    objects, refs, packed-refs, info/alternates or HEAD of the git dir or common dir, and no alternates file exists."""
    if real_dir(spelt) != spelt:
        raise GitError(f"{spelt}: real path differs from the declared path (symlink)")
    dot = os.path.join(spelt, ".git")
    try:
        st = os.lstat(dot)
        if stat.S_ISLNK(st.st_mode):
            raise GitError(f"{spelt}: .git is a symlink")
        if stat.S_ISDIR(st.st_mode):
            gitdir = dot
        else:  # a gitfile of a linked worktree: "gitdir: <path>"
            with open(dot, "rb") as f:
                line = f.read(4096).decode("utf-8", "replace").strip()
            if not line.startswith("gitdir:"):
                raise GitError(f"{spelt}: .git is not a git directory or gitfile")
            gitdir = os.path.join(spelt, line[7:].strip())
        gitdir = real_dir(gitdir)
        common = gitdir
        cf = os.path.join(gitdir, "commondir")
        if os.path.lexists(cf):
            with open(cf, "rb") as f:
                common = real_dir(os.path.join(gitdir, f.read(4096).decode("utf-8", "replace").strip()))
    except OSError as e:
        raise GitError(f"{spelt}: {e}") from e
    siblings = {real_dir(os.path.join(x, ".git")) for x in siblings}
    if not (inside(gitdir, spelt) or common in siblings) or not (inside(common, spelt) or common in siblings) \
            or not inside(gitdir, common):
        raise GitError(f"{spelt}: git dir {gitdir} or common dir {common} is outside the declared path")
    for d in {gitdir, common}:
        walk(d)
        for name in ("objects/info/alternates", "info/alternates"):
            if os.path.lexists(os.path.join(d, name)):
                raise GitError(f"{spelt}: {name} exists (alternate object stores are refused)")
    return spelt


def declare(paths):
    """Check each path declared for one repo, as spelt after ~ expansion; the valid ones are remembered for run().
    Clones with their own .git directory are checked first, and a linked worktree is accepted only through a sibling root
    that itself passed. Returns ([valid paths], [error strings])."""
    spelt = [os.path.abspath(expand(p)) for p in paths]
    good, errors = [], {}
    for s in spelt:
        VALID.pop(s, None)
    for turn in (0, 1):
        for s in spelt:
            if s in good or (turn == 0 and not os.path.isdir(os.path.join(s, ".git"))):
                continue
            sib = tuple(x for x in good if x != s)
            try:
                check(s, sib)
                VALID[s] = sib
                good.append(s)
                errors.pop(s, None)
            except (GitError, OSError) as e:
                errors[s] = str(e)
    return [s for s in spelt if s in good], list(errors.values())


CHECKED = {}


def declared_path(path):
    """The absolute path when declare() accepted it, checked again now (at most every 2 s), else GitError."""
    spelt = os.path.abspath(expand(path))
    if spelt not in VALID:
        raise GitError(f"{path}: not a declared path")
    if time.monotonic() - CHECKED.get(spelt, -10) > 2:
        check(spelt, VALID[spelt])
        CHECKED[spelt] = time.monotonic()
    return spelt


def feed(pipe, data):
    try:
        pipe.write(data)
    except OSError:
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def run(path, *args, cap=CAP, timeout=TIMEOUT, stdin=None):
    """(stdout bytes, truncated) of one allowed git command in a declared path. Raises GitError on a refused
    command, a timeout, or a non-zero exit (a truncated read, killed by us, is not an error). stdin is only for
    cat-file --batch: bytes of 40-hex object ids, one per line, and nothing else."""
    if not args or args[0] not in ALLOWED or (args[0] == "worktree" and args[1:3] != ("list", "--porcelain")):
        raise GitError(f"refused: {args[:1]}")
    if args[0] == "ls-tree" and not (len(args) == 5 and args[1:4] == ("-r", "-z", "--full-tree") and ID.fullmatch(args[4])):
        raise GitError("refused: ls-tree is only -r -z --full-tree <id>")  # no path, no other option, no ref name
    if args[0] == "cat-file" and not (args == ("cat-file", "--batch") and stdin and len(stdin) <= 41 * MAX_BLOBS
                                      and re.fullmatch(rb"(?:[0-9a-f]{40}\n)+", stdin)):
        raise GitError("refused: cat-file is only --batch, fed 40-hex ids")  # no --textconv, no --filters, no ref name
    if stdin is not None and args[0] != "cat-file":
        raise GitError("refused: stdin")
    if args[0] == "log" and not (len(args) == 4 and args[1:3] == ("-1", "--format=%ct") and ID.fullmatch(args[3])) \
            and not (len(args) == 4 and args[1:3] == ("--reverse", "--format=%s") and RANGE.fullmatch(args[3])):
        raise GitError("refused: log is only the timestamp form and the subject form")  # no patch, no path, no other option
    path = declared_path(path)
    argv = [*BASE, "-C", path, args[0], *FLAGS.get(args[0], []), *args[1:]]
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=ENV,
                         stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE)
    out, end, cut = [], time.monotonic() + timeout, False
    size = 0
    try:
        if stdin is not None:  # at most 41 * MAX_BLOBS bytes: it fits the pipe's buffer, and git reads one id at a time
            threading.Thread(target=feed, args=(p.stdin, stdin), daemon=True).start()
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise GitError("timeout")
            if not select.select([p.stdout], [], [], left)[0]:
                continue
            chunk = os.read(p.stdout.fileno(), 1 << 16)
            if not chunk:
                break
            out.append(chunk)
            size += len(chunk)
            if size > cap:
                cut = True
                break
    finally:
        if p.poll() is None:
            p.kill()
        p.wait()
        p.stdout.close()
    if not cut and p.returncode not in (0, 1):
        raise GitError(f"exit {p.returncode}")
    data = b"".join(out)
    return (data[:cap], cut)


def text(path, *args, **kw):
    data, cut = run(path, *args, **kw)
    return data.decode("utf-8", errors="replace"), cut


def resolve(path, ref):
    """The commit id a ref names in this path, or None. Only called with refs Atlas built itself."""
    try:
        out, _ = text(path, "rev-parse", "--verify", "--quiet", ref + "^{commit}")
    except GitError:
        return None
    out = out.strip()
    return out if ID.fullmatch(out) else None


def fetched_at(path):
    """Mtime of the repo's FETCH_HEAD as seconds, None when there is none. A stat, never a write."""
    try:
        common, _ = text(path, "rev-parse", "--git-common-dir")
        common = common.strip()
        common = common if os.path.isabs(common) else os.path.join(declared_path(path), common)
        return os.stat(os.path.join(common, "FETCH_HEAD")).st_mtime
    except (OSError, GitError):
        return None


def branches(path, base_ids):
    """Branch heads in this path not merged into any of base_ids: [{ref, name, id, time, subject, author}]. refs/heads
    and refs/remotes/origin; origin/HEAD is not a branch."""
    fmt = "%(objectname) %(refname) %(committerdate:unix) %(authorname)%09%(subject)"
    sets = []
    for b in base_ids:
        out, _ = text(path, "for-each-ref", f"--no-merged={b}", f"--format={fmt}", "refs/heads", "refs/remotes/origin")
        rows = {}
        for line in out.splitlines():
            head, _, subject = line.partition("\t")
            oid, ref, when, author = head.split(" ", 3)
            if ref == "refs/remotes/origin/HEAD" or not ID.fullmatch(oid):
                continue
            rows[ref] = {"ref": ref, "id": oid, "time": int(when), "author": author, "subject": subject}
        sets.append(rows)
    found = sets[0] if sets else {}
    for rows in sets[1:]:  # not merged into any of the bases: merged into none
        found = {k: v for k, v in found.items() if k in rows}
    for r in found.values():
        r["name"] = r["ref"].removeprefix("refs/heads/").removeprefix("refs/remotes/origin/")
        r["remote"] = r["ref"].startswith("refs/remotes/")
    return list(found.values())


def worktrees(path):
    """{branch name: worktree path} for each worktree with a branch checked out."""
    out, _ = text(path, "worktree", "list", "--porcelain")
    found, cur = {}, {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur.get("branch", "").startswith("refs/heads/"):
                found[cur["branch"].removeprefix("refs/heads/")] = cur["worktree"]
            cur = {}
        else:
            k, _, v = line.partition(" ")
            cur[k] = v
    return found


def commit_time(path, oid):
    """The committer time of a commit id, in seconds, or None."""
    try:
        out, _ = text(path, "log", "-1", "--format=%ct", oid)
        return int(out.strip())
    except (GitError, ValueError):
        return None


def first_subject(path, base, head):
    """The subject of the first commit after base on the way to head (the oldest of base..head), or None. Two ids, a
    fixed format, no path and no patch."""
    if not (ID.fullmatch(base) and ID.fullmatch(head)):
        raise GitError("ids only")
    out, _ = text(path, "log", "--reverse", "--format=%s", f"{base}..{head}", cap=1 << 16)
    return next((l for l in out.splitlines() if l.strip()), None)


def merge_base(path, a, b):
    out, _ = text(path, "merge-base", a, b)
    out = out.strip()
    return out if ID.fullmatch(out) else None


def files(path, base, head):
    """The diff's file list as git gives it for base...head with -M, from --name-status and --numstat, in git's order:
    [{path, old, status, add, del, binary, sensitive}]. base is the merge base id and head an id."""
    if not (ID.fullmatch(base) and ID.fullmatch(head)):
        raise GitError("ids only")
    status, cut1 = run(path, "diff", "--name-status", "-z", "-M", base, head)
    nums, cut2 = run(path, "diff", "--numstat", "-z", "-M", base, head)
    if cut1 or cut2:
        raise GitError("diff too large")
    st, parts, i = {}, status.decode("utf-8", "replace").split("\0"), 0
    while i < len(parts) and parts[i]:
        code = parts[i]
        if code[0] in "RC":
            st[parts[i + 2]] = (code[0], parts[i + 1])
            i += 3
        else:
            st[parts[i + 1]] = (code[0], None)
            i += 2
    out, parts, i = [], nums.decode("utf-8", "replace").split("\0"), 0
    while i < len(parts) and parts[i]:
        add, dele, p = parts[i].split("\t", 2)
        if p == "":  # a rename: old and new follow
            p = parts[i + 2]
            i += 3
        else:
            i += 1
        code, old = st.get(p, ("M", None))
        out.append({"path": p, "old": old, "status": {"A": "added", "D": "deleted", "R": "renamed", "C": "copied"}.get(code, "modified"),
                    "add": None if add == "-" else int(add), "del": None if dele == "-" else int(dele),
                    "binary": add == "-", "sensitive": sensitive(p)})
    return out


HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)")


def parse(raw):
    """Hunks from one file's unified diff: [{o, n, ctx, lines: [[type, old, new, text]]}]; a line before the first
    hunk header is not a hunk line. The '\\ No newline' marker sets nonl on the line before it. Splits on \\n only."""
    hunks, cur, o, n = [], None, 0, 0
    for l in raw.split("\n"):
        m = HUNK.match(l)
        if m:
            o, n = int(m[1]), int(m[3])
            cur = {"o": o, "n": n, "ctx": m[5].strip(), "lines": []}
            hunks.append(cur)
            continue
        if cur is None:
            continue
        if l.startswith("\\"):
            if cur["lines"]:
                cur["lines"][-1].append("nonl")
            continue
        t = l[:1]
        if t == "+":
            cur["lines"].append(["+", None, n, l[1:]]); n += 1
        elif t == "-":
            cur["lines"].append(["-", o, None, l[1:]]); o += 1
        elif t == " ":
            cur["lines"].append([" ", o, n, l[1:]]); o += 1; n += 1
    return hunks


def file_diff(path, base, head, f, context=3):
    """(hunks, truncated) of one file of files(), with `context` lines around each change. A rename names both
    paths so git still pairs them."""
    specs = [":(literal)" + p for p in ([f["old"]] if f["old"] else []) + [f["path"]]]
    raw, cut = text(path, "diff", "-M", f"-U{int(context)}", base, head, "--", *specs)
    return parse(raw), cut


def landed(path, base, head, paths):
    """True when base already holds head's content for every one of these paths (the files head changes since the
    merge base): `diff --name-only base head -- paths` names nothing. A squash merge never makes a branch an
    ancestor of its base, so this is how one is told from open work. Read-only; paths are literal."""
    if not (ID.fullmatch(base) and ID.fullmatch(head)):
        raise GitError("ids only")
    for i in range(0, len(paths), 200):
        out, cut = run(path, "diff", "--name-only", base, head, "--", *(":(literal)" + p for p in paths[i:i + 200]))
        if out or cut:
            return False
    return True


def tree(path, commit):
    """Every blob of a commit as [(id, path)], from `ls-tree -r -z --full-tree <id>`: a file's mode is not read, and an
    entry that is not a blob (a submodule) is dropped. GitError when the listing is cut or the commit is not here."""
    if not ID.fullmatch(commit):
        raise GitError("ids only")
    out, cut = run(path, "ls-tree", "-r", "-z", "--full-tree", commit)
    if cut:
        raise GitError("tree too large")
    found = []
    for rec in out.decode("utf-8", "replace").split("\0"):
        head, _, name = rec.partition("\t")
        parts = head.split(" ")
        if len(parts) == 3 and parts[1] == "blob" and ID.fullmatch(parts[2]):
            found.append((parts[2], name))
    return found


def blobs(path, listed, wanted):
    """{id: bytes or None} for the wanted blob ids, which must all come from `listed` (a tree() result, in this call's
    hands): through `cat-file --batch`, in one process. None is a blob git does not have, or one not read whole."""
    known = {i for i, _ in listed}
    wanted = list(dict.fromkeys(wanted))
    if not all(w in known for w in wanted):
        raise GitError("ids from the tree only")
    got = {}
    for i in range(0, len(wanted), MAX_BLOBS):
        chunk = wanted[i:i + MAX_BLOBS]
        out, cut = run(path, "cat-file", "--batch", stdin="".join(w + "\n" for w in chunk).encode())
        at = 0
        for w in chunk:
            nl = out.find(b"\n", at)
            head = out[at:nl].split(b" ") if nl >= 0 else []
            if len(head) == 2 and head[0].decode("ascii", "replace") == w and head[1] == b"missing":
                got[w], at = None, nl + 1  # git does not have it, and Atlas does not fetch
                continue
            if len(head) != 3 or head[0].decode("ascii", "replace") != w or head[1] != b"blob" or not head[2].isdigit():
                break  # output cut or not what was asked: the rest stay unread
            size = int(head[2])
            body = out[nl + 1:nl + 1 + size]
            got[w] = body if len(body) == size else None
            at = nl + 1 + size + 1
    return {w: got.get(w) for w in wanted}
