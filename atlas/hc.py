"""hc (herdr-core) reads: `ls`, `read` and `get`, through `run`. The one write is `send`, below, for the typist's POST
/api/send: `hc send` and `hc keys` only. Never kill, rm, label, interrupt, attach, server or update, and never --force.
An hc agent's pane is "hc:<name>", which is never a herdr pane id, so every herdr path ignores it. `hc get` is used only
by `space`: its result carries argv twice (top level and foreground), which can hold the environment, so `space` keeps the
one label it needs and drops the rest at once. Atlas never reads, logs or stores argv."""
import json
import subprocess

TIMEOUT = 5
# `hc send` waits for a person to pause typing in that terminal before it pastes, so a send gets longer than a read: 30 s
# fits a normal send; one that waits longer fails and is audited as failed.
SEND_TIMEOUT = 30
PREFIX = "hc:"
INCOMPLETE = "[hc] incomplete:"
# hc's agent to Atlas's runtime word; an agent hc names otherwise is kept as hc names it.
RUNTIMES = {"claude-code": "claude"}
# hc's status to the statuses Atlas already uses; anything else (such as "needs login") is "unknown".
STATUSES = {"working": "working", "blocked": "blocked", "idle": "idle", "done": "done"}


def call(hc, command, args, timeout):
    """One hc command, argv only, stdout back. A missing binary, a timeout or a non-zero exit is a RuntimeError."""
    try:
        p = subprocess.run([hc, command, *args], capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"hc {command}: timed out after {timeout} s")
    except OSError as e:
        raise RuntimeError(f"hc: {e}")
    if p.returncode != 0:
        raise RuntimeError(f"hc {command}: exit {p.returncode}: {(p.stderr or p.stdout).strip()[:200]}")
    return p.stdout


def run(hc, command, *args):
    """One read-only hc command: ls, read or get."""
    if command not in ("ls", "read", "get"):
        raise ValueError(f"hc {command}: Atlas reads hc with ls, read and get only")
    return call(hc, command, args, TIMEOUT)


def send(hc, command, name, arg):
    """The typist's write: `hc send <name> <text>` (a line) or `hc keys <name> <key>` (one key). `--` ends the options, so
    a text or a key that starts with "-" is never read as one. Never --force."""
    if command not in ("send", "keys"):
        raise ValueError(f"hc {command}: Atlas writes to hc with send and keys only")
    return call(hc, command, ["--", name, arg], SEND_TIMEOUT)


def agents(hc):
    """The live agent terminals from `hc ls --json`, as herdr-shaped agent dicts picked by field name: a plain shell
    (agent "env") and a terminal that is not live are left out."""
    out = []
    for t in json.loads(run(hc, "ls", "--json"))["terminals"]:
        if t.get("state") != "live" or not t.get("name") or t.get("agent") in (None, "env"):
            continue
        out.append({"name": t["name"], "pane_id": PREFIX + t["name"], "agent": RUNTIMES.get(t["agent"], t["agent"]),
                    "agent_status": STATUSES.get(t.get("status"), "unknown"), "cwd": t.get("cwd")})
    return out


def read(hc, name, rows=50):
    """An agent's screen, the last rows lines, without hc's "older output may be missing" line."""
    text = run(hc, "read", "--lines", str(rows), name)
    return "\n".join(l for l in text.split("\n") if not l.startswith(INCOMPLETE))


def space(hc, name):
    """The agent's herdr.space label from `hc get`, or None. Only that label is bound to a name; the rest of the result
    is never kept."""
    try:
        label = (json.loads(run(hc, "get", name, "--json")).get("labels") or {}).get("herdr.space")
    except (RuntimeError, ValueError, AttributeError):
        return None
    return label if isinstance(label, str) and label else None
