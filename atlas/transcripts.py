"""Transcript and registry reading. Read only: `*.jsonl` in the stores and the glob `*.json` exactly in the registries
(the `.key` files beside them are never opened). Shell text found in a transcript is scanned, never executed."""
import glob
import json
import os
import re
import shlex
import stat
from datetime import UTC, datetime
from types import SimpleNamespace as NS

from . import db


def roots(var, default):
    return [os.path.expanduser(p) for p in (os.environ.get(var) or default).split(":") if p]


# Each can be pointed elsewhere by the deploy's environment, which is how a store failure is checked.
CLAUDE = roots("ATLAS_CLAUDE_STORES", "~/.claude-org/projects:~/.claude/projects:/data/home/.claude/projects")
CODEX = roots("ATLAS_CODEX_STORE", "~/.codex/sessions")
# The account of a Claude store, by its folder: the two profiles Atlas names; any other store is "other".
ACCOUNT_STORES = {"work": os.path.expanduser("~/.claude-org/projects"), "personal": os.path.expanduser("~/.claude/projects")}
REGISTRIES = roots("ATLAS_REGISTRIES", "~/.claude-org/sessions:~/.claude/sessions:/data/home/.claude/sessions")


def account_of(path):
    return next((k for k, root in ACCOUNT_STORES.items() if path.startswith(root + "/")), "other")


def stamp(text):
    """Epoch seconds from a transcript timestamp, or None."""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def iso(t):
    """UTC with milliseconds, a form that sorts as text."""
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def text_of(content):
    """The plain text of a message's content: a string, or its text blocks joined."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


# Text the runtime writes into a session itself: never a message, and counted as excluded.
RUNTIME_TEXT = ("<pasted_content", "<command-name>", "<command-message>", "<command-args>", "<local-command-",
                "<bash-input>", "<bash-stdout>", "<bash-stderr>", "<task-notification>", "<system-reminder>",
                "<artifact-content-authored-by-others>", "[Request interrupted by user",
                "This session is being continued")
CODEX_RUNTIME_TEXT = ("<environment_context", "# AGENTS.md instructions", "<user_instructions", "<turn_aborted",
                      "<user_shell_command")
# What Claude Code stores for a pasted block or a long line: the text between a wrapper and its twin, one newline each side.
PASTED = re.compile(r'\s*<pasted_content id="([^"]*)">\n(.*)\n</pasted_content id="\1">\s*', re.S)
BLOCK = re.compile(r"<cross-session-message\b([^>]*)>(.*?)</cross-session-message>", re.S)
RECIPIENT = re.compile(r"→ (\S+) \(another Claude session")
CODEX_EXEC = "exec_command({cmd:"
VERB = {"Read": "Reading", "Edit": "Editing", "Write": "Writing"}


def step(content):
    """An agent's own words for the last step in one assistant message, or None: a Bash call's description, Reading,
    Editing or Writing and the file's base name, or Helper: and an Agent or Task description. Never the command,
    any other input, or any output."""
    for b in reversed(content if isinstance(content, list) else ()):
        inp = b.get("input") if isinstance(b, dict) and b.get("type") == "tool_use" else None
        if not isinstance(inp, dict):
            continue
        name, text = b.get("name"), None
        if name == "Bash":
            text = inp.get("description")
        elif name in VERB and isinstance(inp.get("file_path"), str) and inp["file_path"]:
            text = VERB[name] + " " + os.path.basename(inp["file_path"])
        elif name in ("Agent", "Task") and isinstance(inp.get("description"), str) and inp["description"]:
            text = "Helper: " + inp["description"]
        if isinstance(text, str) and text.strip():
            return text.strip()
    return None
CHUNK = 32 << 20


# The quote-aware scan. It cuts raw shell text into simple commands only at ; & | and newlines outside quotes
# (an & right after > or < is a redirection), and gives up on text it does not follow: a heredoc, an open quote.
def simple_commands(src):
    out, cur, i, quote = [], [], 0, None
    while i < len(src):
        c = src[i]
        if quote == "'":
            quote = None if c == "'" else quote
        elif c == "\\":
            cur.append(src[i:i + 2])
            i += 2
            continue
        elif quote == '"':
            quote = None if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif c == "#" and (not cur or cur[-1] in " \t"):
            j = src.find("\n", i)  # a comment runs to the end of its line
            i = len(src) if j < 0 else j
            continue
        elif src.startswith("<<", i):
            return None
        elif c in ";&|\n" and not (c == "&" and cur and cur[-1] in "<>"):
            out.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    if quote:
        return None
    out.append("".join(cur))
    return [c.strip() for c in out if c.strip()]


def is_herdr(word):
    return word == "herdr" or word.endswith("/herdr") and (word.startswith("/") or word.startswith("~/.local/bin/"))


def herdr_calls(src):
    """(verb, raw, args) for each simple command that is `herdr agent prompt` or `herdr agent start`.
    A source holding a heredoc, `bash -c`, `eval` or a function gives none at all."""
    cmds = simple_commands(src)
    if cmds is None:
        return []
    parsed = []
    for raw in cmds:
        try:
            words = shlex.split(raw)
        except ValueError:
            return []
        if not words:
            continue
        if (words[0] in ("eval", "function") or "()" in raw
                or os.path.basename(words[0]) in ("bash", "sh", "zsh", "dash") and "-c" in words):
            return []
        parsed.append((raw, words))
    return [(w[2], raw, w[3:]) for raw, w in parsed
            if len(w) >= 3 and is_herdr(w[0]) and w[1] == "agent" and w[2] in ("prompt", "start")]


def positionals(args, valued):
    """Positional words, skipping options; those in valued take the next word. Stops at --."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            break
        if a in valued:
            i += 2
            continue
        if not a.startswith("--"):
            out.append(a)
        i += 1
    return out


def option(args, name):
    for i, a in enumerate(args[:-1]):
        if a == "--":
            break
        if a == name:
            return args[i + 1]
    return None


def expands(word):
    return "$" in word or "`" in word


class Evidence:
    """What the matchers need, held in memory: full texts and offsets never reach the database.
    Everything is keyed by session and event id, never by path, so a session copied into two stores is one."""

    def __init__(self, since):
        self.since = since  # epoch seconds: events before it are not kept
        self.files = {}  # path -> [inode, offset, rollout id, rollout cwd]
        self.sessions = {}  # session -> NS(runtime, first, last, cwd, launch_at, launch_cwd, now, now_at, account, model, model_at)
        self.sends, self.results, self.blocks, self.prompts = {}, {}, {}, {}
        self.occurrences, self.launches, self.excluded = {}, {}, {}
        self.account = None  # the account of the file being read

    def prune(self, since):
        self.since = since
        for d in (self.sends, self.results, self.blocks, self.prompts, self.occurrences, self.launches):
            for k in [k for k, v in d.items() if v.t < since]:
                del d[k]
        for k in [k for k, t in self.excluded.items() if t < since]:
            del self.excluded[k]

    def read(self, path, runtime):
        """Read the complete lines past this file's offset. Raises OSError when the file cannot be read."""
        st = os.stat(path)
        f = self.files.get(path)
        if f is None or f[0] != st.st_ino or st.st_size < f[1]:
            f = self.files[path] = [st.st_ino, 0, None, None]
        if st.st_size == f[1]:
            return
        self.account = account_of(path)
        with open(path, "rb") as fh:
            fh.seek(f[1])
            while f[1] < st.st_size:
                data = fh.read(min(CHUNK, st.st_size - f[1]))
                end = data.rfind(b"\n") + 1  # a partial last line waits for the next read
                if not end:
                    if len(data) < CHUNK:
                        break
                    data += fh.readline()  # one line longer than a chunk
                    end = data.rfind(b"\n") + 1
                    if not end:
                        break
                lines = data[:end].splitlines()
                for line in lines:
                    if runtime == "claude":
                        self.claude_line(line)
                    else:
                        self.codex_line(line, f)
                self.last_event(lines, runtime, f)
                f[1] += end
                fh.seek(f[1])

    def last_event(self, lines, runtime, f):
        """A session's last activity is the last timestamped event in its file, whatever its type. The fast filters
        skip most lines, so the last one is parsed here, once per read."""
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = stamp(e.get("timestamp")) if isinstance(e, dict) else None
            sid = e.get("sessionId") if runtime == "claude" else f[2]
            if t is not None and isinstance(sid, str):
                self.seen(sid, runtime, t, e.get("cwd") if runtime == "claude" else f[3])
                return

    def seen(self, sid, runtime, t, cwd):
        """first and last event; cwd is the latest, launch_cwd the cwd of the earliest event that has one."""
        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = NS(runtime=runtime, first=t, last=t, cwd=cwd, launch_at=None, launch_cwd=None,
                                        now=None, now_at=None, account=None, model=None, model_at=None)
        s.first = min(s.first, t)
        if t >= s.last:
            s.last, s.cwd = t, cwd or s.cwd
        if cwd and (s.launch_at is None or t < s.launch_at):
            s.launch_at, s.launch_cwd = t, cwd

    def claude_line(self, line):
        # Most lines are neither a message nor a tool call; skip them before parsing.
        if b'"type":"user"' not in line and b'"type":"assistant"' not in line:
            return
        try:
            e = json.loads(line)
        except ValueError:
            return
        sid, t = e.get("sessionId"), stamp(e.get("timestamp"))
        if not isinstance(sid, str) or t is None or e.get("isSidechain"):
            return
        self.seen(sid, "claude", t, e.get("cwd"))
        msg = e.get("message") if isinstance(e.get("message"), dict) else {}
        content, uuid = msg.get("content"), e.get("uuid")
        s = self.sessions[sid]
        model = msg.get("model")
        if e.get("type") == "assistant" and isinstance(model, str) and model != "<synthetic>" and (s.model_at is None or t >= s.model_at):
            s.account, s.model, s.model_at = self.account, model, t  # the latest reply's model, and the store it was read in
        if e.get("type") == "assistant" and (s.now_at is None or t >= s.now_at) and (words := step(content)):
            s.now, s.now_at = words, t
        if t < self.since:
            return
        if e.get("type") == "assistant":
            for b in content if isinstance(content, list) else ():
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                if b.get("name") == "SendMessage" and isinstance(inp.get("message"), str):
                    to = inp.get("to") if isinstance(inp.get("to"), str) else None
                    self.sends[(sid, b.get("id"))] = NS(key=f"send:{sid}:{b.get('id')}", id=b.get("id"), sid=sid,
                                                         t=t, to=to, text=inp["message"])
                elif b.get("name") == "Bash" and isinstance(inp.get("command"), str):
                    self.commands(sid, t, b.get("id"), [inp["command"]])
            return
        if e.get("type") != "user":
            return
        result = e.get("toolUseResult")
        if isinstance(content, list) and isinstance(result, dict) and "success" in result:
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    named = RECIPIENT.findall(str(result.get("message", "")))
                    self.results[b.get("tool_use_id")] = NS(ok=bool(result["success"]), to=named[-1] if named else None, t=t)
            return
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return
        text = text_of(content)
        if not text.strip() or not isinstance(uuid, str):
            return
        origin = e.get("origin") if isinstance(e.get("origin"), dict) else {}
        if origin.get("kind") == "peer":  # the runtime's own mark on a delivered cross-session message
            for i, m in enumerate(BLOCK.finditer(text)):
                attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(1)))
                self.blocks[(sid, uuid, i)] = NS(key=f"recv:{sid}:{uuid}:{i}", sid=sid, t=t, cwd=e.get("cwd"),
                                                 frm=attrs.get("from-name"), sock=attrs.get("from"), text=m.group(2))
            return
        if (e.get("isMeta") or e.get("isCompactSummary") or e.get("isVisibleInTranscriptOnly")
                or origin.get("kind") not in (None, "human") or text.lstrip().startswith(RUNTIME_TEXT)):
            self.excluded[(sid, uuid)] = t
            return
        self.prompts[(sid, uuid)] = NS(key=f"prompt:{sid}:{uuid}", sid=sid, t=t, cwd=e.get("cwd"), text=text)

    def codex_line(self, line, f):
        if not (b'"session_meta"' in line or b'"role":"user"' in line or
                b'"custom_tool_call"' in line and b"herdr" in line):
            return
        try:
            e = json.loads(line)
        except ValueError:
            return
        p, t = e.get("payload") if isinstance(e.get("payload"), dict) else {}, stamp(e.get("timestamp"))
        if e.get("type") == "session_meta":
            f[2], f[3] = p.get("id"), p.get("cwd")
        sid, ordinal = f[2], e.get("ordinal")
        if not isinstance(sid, str) or t is None:
            return
        self.seen(sid, "codex", t, f[3])
        if t < self.since or e.get("type") != "response_item":
            return
        key = f"{sid}:{ordinal}"
        if p.get("type") == "custom_tool_call" and p.get("name") == "exec" and isinstance(p.get("input"), str):
            self.commands(sid, t, key, codex_shell(p["input"]))
        elif p.get("type") == "message" and p.get("role") == "user":
            meta = p.get("internal_chat_message_metadata_passthrough")
            kinds = meta.get("content_item_kinds") if isinstance(meta, dict) else None
            texts = []
            for i, b in enumerate(p.get("content") or []):
                text = b.get("text") if isinstance(b, dict) and b.get("type") == "input_text" else None
                if not isinstance(text, str) or not text.strip():
                    continue
                kind = kinds[i] if isinstance(kinds, list) and i < len(kinds) else None
                if kind == "user.text" or kind is None and not text.lstrip().startswith(CODEX_RUNTIME_TEXT):
                    texts.append(text)
            if texts:
                self.prompts[(sid, key)] = NS(key=f"prompt:{key}", sid=sid, t=t, cwd=f[3], text="\n".join(texts))
            else:
                self.excluded[(sid, key)] = t

    def commands(self, sid, t, call, sources):
        """Each `herdr agent prompt` and `herdr agent start` occurrence in one tool call, numbered within it."""
        n = 0
        for src in sources:
            if "herdr" not in src:
                continue
            for verb, raw, args in herdr_calls(src):
                key = (sid, call, n)
                n += 1
                if verb == "prompt":
                    words = positionals(args, ("--until", "--timeout"))
                    if len(words) < 2 or expands(words[0]):
                        continue
                    target, text = words[0], words[1]
                    literal = not expands(raw)
                    if not literal:
                        text = re.split(r"[$`]", text, maxsplit=1)[0]
                        if len(" ".join(text.split())) < 20:
                            continue
                    self.occurrences[key] = NS(key=key, sid=sid, t=t, target=target, text=" ".join(text.split()),
                                               literal=literal)
                else:
                    words = positionals(args, ("--kind", "--pane", "--timeout"))
                    pane = option(args, "--pane")
                    if words and not expands(words[0]):  # the pane may be a shell variable: kept as None
                        self.launches[key] = NS(key=key, sid=sid, t=t, name=words[0],
                                                pane=pane if pane and not expands(pane) else None)


def codex_shell(inp):
    """The shell commands in a Codex exec input: only a JSON string literal right after `exec_command({cmd:`,
    ending at a comma or brace, and only where the marker is code: not inside a comment or a string.
    A template string or a concatenation gives nothing; an unterminated comment or string gives nothing at all."""
    out, i, n = [], 0, len(inp)
    while i < n:
        if inp.startswith("//", i):
            j = inp.find("\n", i)
            i = n if j < 0 else j + 1
        elif inp.startswith("/*", i):
            j = inp.find("*/", i + 2)
            if j < 0:
                return []
            i = j + 2
        elif inp[i] in "'\"`":
            j = i + 1
            while j < n and inp[j] != inp[i]:
                j += 2 if inp[j] == "\\" else 1
            if j >= n:
                return []
            i = j + 1
        elif inp.startswith(CODEX_EXEC, i):
            i += len(CODEX_EXEC)
            j = len(inp) - len(inp[i:].lstrip())
            if inp.startswith('"', j):
                try:
                    s, end = json.JSONDecoder().raw_decode(inp, j)
                except ValueError:
                    continue  # the quote is scanned as a string from here
                if isinstance(s, str) and inp[end:].lstrip()[:1] in (",", "}"):
                    out.append(s)
                i = end
        else:
            i += 1
    return out


def store_files(root, runtime):
    """(paths, errors): the transcript files under one root, `*/*.jsonl` for Claude and every `*.jsonl` below it for
    Codex, and each directory that could not be listed. Raises OSError when the root itself cannot be listed.
    An empty directory is valid; a directory that cannot be listed is an error, never an empty one."""
    paths, errors, todo = [], [], [(root, 0)]
    while todo:
        d, depth = todo.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError as e:
            if d == root:
                raise
            errors.append(f"{d}: {e.strerror or e}")
            continue
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir(follow_symlinks=runtime == "claude"):
                    if runtime == "codex" or depth == 0:
                        todo.append((e.path, depth + 1))
                elif e.name.endswith(".jsonl") and (runtime == "codex" or depth == 1):
                    paths.append(e.path)
            except OSError as err:
                errors.append(f"{e.path}: {err.strerror or err}")
    return paths, errors


def read_stores(ev, since):
    """One pass over every store: files modified since `since`, new bytes only. Returns {source: (state, error, failed)}."""
    health = {}
    for source, rootlist in (("claude", CLAUDE), ("codex", CODEX)):
        failed, errors = 0, []
        for root in rootlist:
            try:
                paths, unlisted = store_files(root, source)
            except OSError as e:
                health[source] = ("failing", f"{root}: {e.strerror or e}", 0)
                break
            failed += len(unlisted)
            errors += unlisted
            for path in paths:
                try:
                    if os.stat(path).st_mtime >= since:
                        ev.read(path, source)
                except OSError as e:
                    failed += 1
                    errors.append(f"{path}: {e.strerror or e}")
        else:
            health[source] = ("degraded", errors[0], failed) if failed else ("ok", None, 0)
    return health


class RegistryPoller:
    """Records each registered messaging name, its socket and session, with the time the registry says it was taken."""
    interval = 5

    def __init__(self, conn):
        self.conn = conn

    def poll(self):
        at, rows, failed, error = db.now(), [], 0, None
        for root in REGISTRIES:
            try:
                os.scandir(root).close()
            except OSError as e:
                with self.conn:
                    db.set_health(self.conn, "registry", at, "failing", f"{root}: {e.strerror or e}")
                return
            for path in glob.glob(os.path.join(glob.escape(root), "*.json")):
                try:
                    with open(path, encoding="utf-8") as fh:
                        r = json.load(fh)
                    since = r.get("nameSince") or r.get("startedAt")
                    rows.append((r["sessionId"], r["name"], r.get("messagingSocketPath") or "",
                                 datetime.fromtimestamp(since / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if since else None))
                except FileNotFoundError:
                    continue  # the process ended between the listing and the read
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
                    failed, error = failed + 1, f"{path}: {e}"
        with self.conn:
            self.conn.executemany(
                """INSERT INTO session_names (session_id, registry_name, socket, name_since, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (session_id, registry_name, socket) WHERE registry_name IS NOT NULL
                   DO UPDATE SET last_seen = excluded.last_seen,
                                 name_since = coalesce(session_names.name_since, excluded.name_since)""",
                [(*r, at, at) for r in rows if isinstance(r[0], str) and isinstance(r[1], str)],
            )
            db.set_health(self.conn, "registry", at, *(("degraded", error, failed) if failed else ()))


# ---- History: one agent's transcript as a conversation, for its terminal view (spec 2026-10-31-history-and-fit) ----
# What it was asked (with who asked, where the transcript says), its replies as text, and one line per tool call. No
# thinking and no tool results: results can hold secrets and file contents. Read only, never run.

SESSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{7,63}")  # a session id is a plain name: never a path
HISTORY_ITEMS = 200  # items per answer; whole lines, so one answer can hold a few more
HISTORY_READ_MAX = 16 << 20  # bytes read back from the end or the cursor per answer
HISTORY_TEXT_MAX = 16000  # characters of one item's text
HISTORY_CHUNK = 1 << 20
TOOL_KEYS = ("description", "file_path", "command", "pattern", "url", "query", "prompt")
_found = {}  # (runtime, session) -> (path or None, monotonic time of the look)


def transcript_path(runtime, sid, now):
    """The transcript of one session as (store root, path below it), for files.open_below to open by descriptors, or
    None. A Claude session is <store>/<project>/<session>.jsonl, a Codex one a *-<session>.jsonl below its store; no
    symlink is followed, to a folder or to the file (PR 38 review B). A miss is asked again after 30 s."""
    if runtime not in ("claude", "codex") or not isinstance(sid, str) or not SESSION.fullmatch(sid):
        return None
    hit = _found.get((runtime, sid))
    if hit and (hit[0] and plain_file(os.path.join(*hit[0])) or not hit[0] and now - hit[1] < 30):
        return hit[0]
    found = None
    for root in CLAUDE if runtime == "claude" else CODEX:
        try:
            if runtime == "claude":
                with os.scandir(root) as it:
                    names = [e.name for e in it if not e.name.startswith(".") and e.is_dir(follow_symlinks=False)]
                found = next((f"{n}/{sid}.jsonl" for n in names if plain_file(os.path.join(root, n, sid + ".jsonl"))), None)
            else:
                paths, _ = store_files(root, "codex")  # a Codex store's folders are never followed through a symlink
                found = next((os.path.relpath(p, root) for p in paths if os.path.basename(p).endswith(f"-{sid}.jsonl")), None)
        except OSError:
            continue
        if found:
            found = (root, found)
            break
    _found[(runtime, sid)] = (found, now)
    return found


def plain_file(path):
    """A regular file that is not a symlink."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def one_line(tool, inp):
    """A tool call as one line, in the agent's own words where it gave them: `Bash: <its description>`, and a message
    as `SendMessage to <recipient>: <its summary>` (PR 38 UI review S1)."""
    if isinstance(inp, dict) and isinstance(inp.get("to"), str) and inp["to"].strip():
        line = f"{tool} to {inp['to'].strip().splitlines()[0][:60]}"
        summary = inp.get("summary") if isinstance(inp.get("summary"), str) else ""
        return f"{line}: {summary.strip().splitlines()[0][:110]}" if summary.strip() else line
    for k in TOOL_KEYS:
        v = inp.get(k) if isinstance(inp, dict) else None
        if isinstance(v, list) and all(isinstance(w, str) for w in v):
            v = " ".join(v)
        if isinstance(v, str) and v.strip():
            return f"{tool}: {v.strip().splitlines()[0][:110]}"
    return str(tool)


def typed(text):
    """What a person typed or pasted, from a user message's text: the inside of a pasted-content wrapper, or the text
    itself; "" for blank text and for the runtime's own text, which is never a message."""
    m = PASTED.fullmatch(text)
    if m:
        return m.group(2)
    return "" if not text.strip() or text.lstrip().startswith(("<",) + RUNTIME_TEXT) else text


def claude_items(e, who):
    # A delivered SendMessage is marked meta by the runtime, yet it is a message the agent received: it is kept.
    origin = e.get("origin") if isinstance(e.get("origin"), dict) else {}
    if e.get("isSidechain") or e.get("isCompactSummary") or e.get("isVisibleInTranscriptOnly") \
            or e.get("isMeta") and origin.get("kind") != "peer":
        return []
    at = e.get("timestamp")
    if e.get("type") == "attachment":
        # A line that arrived while the agent worked: queued, and read at its next step. Its prompt is stored as typed.
        a = e.get("attachment") if isinstance(e.get("attachment"), dict) else {}
        o = a.get("origin") if isinstance(a.get("origin"), dict) else {}
        text = typed(text_of(a.get("prompt"))) if a.get("type") == "queued_command" and o.get("kind") in (None, "human") else ""
        return [("in", who(None, text), at, text.strip())] if text.strip() else []
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    content = msg.get("content")
    if e.get("type") == "user":
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return []
        text = text_of(content)
        if origin.get("kind") == "peer":
            m = BLOCK.search(text)
            body = origin["body"] if isinstance(origin.get("body"), str) else m.group(2) if m else text
            return [("in", who(origin, None), at, body.strip())] if body.strip() else []
        if origin.get("kind") not in (None, "human"):
            return []
        text = typed(text)
        return [("in", who(None, text), at, text.strip())] if text else []
    out = []
    for b in content if e.get("type") == "assistant" and isinstance(content, list) else ():
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip():
            out.append(("out", None, at, b["text"].strip()))
        elif isinstance(b, dict) and b.get("type") == "tool_use":
            out.append(("tool", None, at, one_line(b.get("name"), b.get("input"))))
    return out


def codex_items(e, who):
    p, at = e.get("payload") if isinstance(e.get("payload"), dict) else {}, e.get("timestamp")
    if e.get("type") != "response_item":
        return []
    kind = p.get("type")
    if kind == "message" and p.get("role") in ("user", "assistant"):
        want = "input_text" if p["role"] == "user" else "output_text"
        texts = [b["text"] for b in p.get("content") or [] if isinstance(b, dict) and b.get("type") == want
                 and isinstance(b.get("text"), str) and b["text"].strip()]
        if p["role"] == "user":
            texts = [t for t in texts if not t.lstrip().startswith(CODEX_RUNTIME_TEXT + ("<",))]
            return [("in", who(None, t), at, t.strip()) for t in texts]
        return [("out", None, at, t.strip()) for t in texts]
    if kind == "function_call":
        try:
            args = json.loads(p.get("arguments") or "{}")
        except (TypeError, ValueError):
            args = {}
        return [("tool", None, at, one_line(p.get("name"), args))]
    if kind == "custom_tool_call":
        cmds = codex_shell(p["input"]) if p.get("name") == "exec" and isinstance(p.get("input"), str) else []
        return [("tool", None, at, one_line(p.get("name"), {"command": cmds[0]} if cmds else {}))]
    return []


def wanted(line, runtime):
    """The fast filter keeps every line whose top-level type could hold an item; it never drops a line for what its
    text says (PR 38 review S). Items are chosen from the parsed line's types alone."""
    if runtime == "claude":
        return b'"type":"user"' in line or b'"type":"assistant"' in line or b'"queued_command"' in line
    return b'"response_item"' in line


def history(fd, runtime, before, who):
    """From an open transcript (the caller opens it with files.open_below and closes it): the last HISTORY_ITEMS
    items that start before byte `before` (the end when None), oldest first, and the cursor for the items before them
    (None at the start of the file). Reads back from there in chunks, at most HISTORY_READ_MAX bytes; a line still
    being written at the end waits for the next answer."""
    size = os.fstat(fd).st_size
    end = size if before is None else min(before, size)
    found, pos, carry, partial, earlier = [], end, b"", True, None
    while pos > 0 and end - pos < HISTORY_READ_MAX and sum(len(x[1]) for x in found) < HISTORY_ITEMS:
        n = min(HISTORY_CHUNK, pos, HISTORY_READ_MAX - (end - pos))
        pos -= n
        buf = os.pread(fd, n, pos) + carry
        if partial:  # the bytes after the last newline are a line not yet finished
            cut = buf.rfind(b"\n")
            if cut < 0:
                carry = b""
                continue
            buf, partial = buf[:cut], False
        parts, offs, o = buf.split(b"\n"), [], pos
        for part in parts:
            offs.append(o)
            o += len(part) + 1
        carry = parts.pop(0) if pos > 0 else b""
        if pos > 0:
            offs.pop(0)
        for off, line in reversed(list(zip(offs, parts))):
            if not wanted(line, runtime):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            items = (claude_items if runtime == "claude" else codex_items)(e, who) if isinstance(e, dict) else []
            if items:
                found.append((off, items))
                if sum(len(x[1]) for x in found) >= HISTORY_ITEMS:
                    break
    if found and (pos > 0 or found[-1][0] > 0) and (sum(len(x[1]) for x in found) >= HISTORY_ITEMS or pos > 0):
        earlier = found[-1][0]
    elif pos > 0:
        earlier = pos
    out = []
    for off, items in reversed(found):
        for i, (kind, sender, at, text) in enumerate(items):
            out.append({"id": [off, i], "kind": kind, "who": sender, "at": at,
                        "text": text if len(text) <= HISTORY_TEXT_MAX else text[:HISTORY_TEXT_MAX] + " …",
                        **({"n": len(text)} if kind == "in" else {})})  # n: the whole text's length, for web.history's mark
    return out, earlier
