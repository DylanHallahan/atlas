"""What /usage shows besides the account cards: which account and model each live agent runs on, and each repo's
linked worktrees. Account and model are stored per agent by the transcript poller; worktrees are read-only git, every 10
minutes. Nothing here opens a profile's settings or credentials, and nothing is fetched."""
import re
import threading
import time

from . import git

ACCOUNTS = {"work": "Work Claude", "personal": "Personal Claude", "other": "Claude (other profile)"}
MODEL = re.compile(r"claude-(opus|sonnet|haiku)-(\d+)-(\d+)")
STALE_DAYS, TTL, FAILED_TTL = 2, 600, 60
UNREAD = "Claude · not read yet"


def model_name(model):
    """claude-opus-5-5 is Opus 5.5; any other id is shown as written."""
    m = MODEL.fullmatch(model or "")
    return f"{m[1].title()} {m[2]}.{m[3]}" if m else model


def runs_on(a):
    """The line for one agent: "Personal Claude · Opus 5.5", "Codex", or "Claude · not read yet" for a Claude agent
    with no account or model read, or None for a runtime Atlas does not know."""
    if a["runtime"] == "codex":
        return "Codex"
    if a["runtime"] != "claude":
        return None
    if not a["account"]:
        return UNREAD
    return f"{ACCOUNTS.get(a['account'], ACCOUNTS['other'])} · {model_name(a['model'])}"


def agents(rows):
    """The live agents (state rows with runs_on set) as one row per account and model, most agents first, the
    accounts of Work and Personal with no live agent, and the workers' counts by model."""
    live = [a for a in rows if a["gone"] is False and a["runs_on"]]
    groups = {}
    for a in live:
        groups.setdefault(a["runs_on"], []).append(a["name"])
    workers = {}
    for a in live:
        model = a["runs_on"].rpartition(" · ")[2]
        if a["mascot_kind"] == "worker":
            workers[model] = workers.get(model, 0) + 1
    rows = [{"label": k, "names": sorted(v)} for k, v in sorted(groups.items(), key=lambda kv: (kv[0] == UNREAD, -len(kv[1]), kv[0]))]
    quiet = [name for key, name in ACCOUNTS.items() if key != "other" and not any(a["runs_on"].startswith(name) for a in live)]
    if any(a["runs_on"] == UNREAD for a in live):  # an unread agent may be on any account or model: say nothing it could falsify
        return {"rows": rows, "empty": [], "workers": None}
    return {"rows": rows, "empty": quiet, "workers": workers}


def worktrees_of(path, now):
    """(count, untouched, oldest age in days) of a repo's linked worktrees; the main checkout, listed first, is left out."""
    out, _ = git.text(path, "worktree", "list", "--porcelain")
    found, cur = [], {}
    for line in out.splitlines() + [""]:
        if line:
            k, _, v = line.partition(" ")
            cur[k] = v
        elif cur:
            found.append(cur)
            cur = {}
    linked = found[1:]
    ages = []
    for w in linked:
        head = w.get("HEAD", "")
        t = git.commit_time(path, head) if git.ID.fullmatch(head) else None
        if t is not None:
            ages.append((now - t) / 86400)
    return len(linked), sum(a >= STALE_DAYS for a in ages), max(ages) if ages else None


LOCK, HELD = threading.Lock(), {}  # repo -> (monotonic time of the read, its row or None, whether it failed)


def worktrees(app):
    """{rows, failed}: one row per repo with linked worktrees, most first, each repo read at most every 10 minutes from
    the clones the review poll has already checked; failed names the repos whose read failed, each read again after 60 s."""
    with LOCK:
        snap, rows, failed, now = app.review.snap, [], [], time.time()
        for name, r in (snap["repos"] if snap else {}).items():
            held = HELD.get(name)
            if held and time.monotonic() - held[0] < (FAILED_TTL if held[2] else TTL):
                row = held[1]
                if held[2]:
                    failed.append(name)
            elif not r["clones"]:
                continue
            else:
                try:
                    n, stale, oldest = worktrees_of(r["clones"][0][0], now)
                except git.GitError:
                    failed.append(name)
                    HELD[name] = (time.monotonic(), None, True)
                    continue
                row = {"repo": name, "count": n, "untouched": stale, "oldest_days": oldest} if n else None
                HELD[name] = (time.monotonic(), row, False)
            if row:
                rows.append(row)
        rows.sort(key=lambda x: (-x["count"], x["repo"]))
        return {"rows": rows, "failed": failed}
