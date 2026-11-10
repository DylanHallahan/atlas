"""RainCLI, read only. Five commands, each with --json, from one fixed list: conversations, thread, people, groups and connector
status. Nothing else is ever run (never whoami, send, reply, ack, fetch, watch, archive, login or config).

The poller keeps in the database only what the org chart's cards need: who is waiting on our reply and since when, whether
the person ever wrote, and presence. A thread's messages are read to find that, and their text is dropped at once. The
thread page reads its messages live when it is opened and keeps none of them: not in the database, a log or a cache."""
import json
import os
import re
import subprocess
import time

from . import db

TIMEOUT, THREAD_TIMEOUT = 60, 120  # a thread is paged 100 messages a request and one read of 421 took 47 s
CONNECTOR = os.path.expanduser("~/.config/raincli/connector.json")  # a path handed to the CLI, which reads it; never opened here
READS = {"conversations": ("conversations", "--all", "--json"),
         "people": ("people", "--json"),
         "groups": ("groups", "--json"),
         "status": ("connector", "status", "--config", CONNECTOR, "--json")}
ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
ADDRESS = re.compile(r"[^\s<>()\[\]\"',;:]+@[^\s<>()\[\]\"',;]+")  # an email address in a message, never shown
# The connector's own hold reasons to RainCLI's API codes: presentation.py _GROUPS, then _API_CODES. Any other is "failed".
GROUPS = {"no_inbox": "no_inbox", "busy": "busy", "blocked": "busy", "next_turn": "next_turn", "offline": "offline",
          "expired_offline": "offline", "agent_not_ready": "offline", "client_update_needed": "update",
          "approval_required": "approval", "sender_blocked": "approval"}
API_CODES = {"no_inbox": "no_inbox", "busy": "inbox_busy", "next_turn": "inbox_next_turn", "offline": "inbox_offline",
             "update": "update_needed", "approval": "approval", "failed": "inbox_failed"}
HOLD_STATES = ("held", "agent_held")  # queue.HOLD_STATES; handed_over is not counted
mask = lambda text: ADDRESS.sub("[address]", text) if isinstance(text, str) else text
UNREACHABLE = "RainCLI not reachable"


class TimedOut(RuntimeError):
    """A read that passed its timeout: said apart from a failed one."""


def argv(read, conversation=None):
    """The command line of one of the four reads. A thread takes a conversation id, checked to be plain."""
    if read == "thread":
        if not conversation or not ID.fullmatch(conversation):
            raise ValueError("conversation id")
        return ("thread", conversation, "--json")
    return READS[read]


def run(binary, read, conversation=None):
    """Run one read and return its stdout. A missing binary, a timeout or a non-zero exit is a RuntimeError that names
    the read and the exit code only: never the output, which can hold what a person wrote."""
    try:
        p = subprocess.run([binary, *argv(read, conversation)], capture_output=True, text=True, errors="replace", timeout=THREAD_TIMEOUT if read == "thread" else TIMEOUT)
    except subprocess.TimeoutExpired:
        raise TimedOut(f"raincli {read}: timed out")
    except OSError:
        raise RuntimeError(f"raincli {read}: cannot run")
    if p.returncode != 0:
        raise RuntimeError(f"raincli {read}: exit {p.returncode}")
    return p.stdout


def thread(binary, conversation):
    """A thread's messages, oldest first, as RainCLI gives them. In memory only."""
    out = []
    for line in run(binary, "thread", conversation).splitlines():
        if line.strip():
            m = json.loads(line)
            out.append(m.get("message", m) if isinstance(m, dict) else {})
    return out


def ours(m, name):
    """A message is ours when it was not sent by the conversation's person, named by display name (never an address).
    name is the masked name; RainCLI falls back to an email address when a person has no display name."""
    e = m.get("from_endpoint") or {}
    return not ("person" in e and mask(e.get("display_name")) == name)


def waiting(messages, name):
    """(waiting_since, wrote): the time of their first message after our last one when the last message is theirs, else
    None; and whether any message in the thread is theirs. Seq order, times only. A held reply of ours is our reply."""
    ms = sorted(messages, key=lambda m: m.get("seq") or 0)
    theirs = [not ours(m, name) for m in ms]
    since = None
    if theirs and theirs[-1]:
        i = len(ms) - 1
        while i > 0 and theirs[i - 1]:
            i -= 1
        since = ms[i].get("created_at")
        if not since:
            raise ValueError("created_at")  # a failed read, never "nothing waiting"
    return since, any(theirs)


class Poller:
    """Conversations and the connector's status every 2 minutes, people and groups every 5, a thread only when its conversation's
    last message changed."""
    interval = 120
    PEOPLE_EVERY = 300

    def __init__(self, conn, binary):
        self.conn, self.binary = conn, binary
        self.people, self.groups, self.people_at = {}, [], 0.0

    def poll(self):
        at = db.now()
        try:
            self.read(at)
        except (RuntimeError, ValueError, KeyError, TypeError) as e:
            err = str(e) if isinstance(e, RuntimeError) else f"unreadable answer: {type(e).__name__}"
            with self.conn:
                db.set_health(self.conn, "raincli", at, "failing", err)
            return
        with self.conn:
            db.set_health(self.conn, "raincli", at)

    def read(self, at):
        """The reads that fail the pass come first. Then each thread is read and its card written at once, so a thread that
        fails or is slow leaves its own card marked (failed, keeping its old counts) and never the others."""
        convs = [c for c in json.loads(run(self.binary, "conversations"))["conversations"] if not c.get("archived")]
        if time.monotonic() - self.people_at >= self.PEOPLE_EVERY or not self.people:
            self.people = {p["display_name"]: p.get("presence") for p in json.loads(run(self.binary, "people"))["people"]}
            self.groups = [(g["id"], mask(g["name"]), int(g.get("members_n") or 0), int(bool(g.get("taking_part"))))
                           for g in json.loads(run(self.binary, "groups"))["groups"]]
            self.people_at = time.monotonic()
        status = json.loads(run(self.binary, "status"))
        held = {}
        for m in status.get("messages") or []:
            if m.get("state") in HOLD_STATES:
                code = API_CODES[GROUPS.get(m.get("hold_reason"), "failed")] if m.get("hold_reason") else "held"
                held[code] = held.get(code, 0) + 1
        with self.conn:
            self.conn.execute("DELETE FROM raincli_groups")
            self.conn.executemany("INSERT INTO raincli_groups (id, name, members, taking_part) VALUES (?, ?, ?, ?)", self.groups)
            self.conn.execute("DELETE FROM raincli_cards WHERE id NOT IN (SELECT value FROM json_each(?))", (json.dumps([c["id"] for c in convs]),))
            self.conn.execute(
                """INSERT INTO raincli_inbox (one, mode, herdr_agent, escalation_target, held, read_at) VALUES (1, ?, ?, ?, ?, ?)
                   ON CONFLICT (one) DO UPDATE SET mode = excluded.mode, herdr_agent = excluded.herdr_agent,
                       escalation_target = excluded.escalation_target, held = excluded.held, read_at = excluded.read_at""",
                (status.get("mode"), status.get("herdr_agent"), status.get("escalation_target"), json.dumps(held), at))
        stored = {r["id"]: r for r in self.conn.execute("SELECT id, last_seq, failed FROM raincli_cards")}
        for c in convs:
            old, name, presence = stored.get(c["id"]), mask(c["peer"]), self.people.get(c["peer"])
            if old is not None and old["last_seq"] == c["last_seq"] and not old["failed"]:
                with self.conn:
                    self.conn.execute("UPDATE raincli_cards SET name = ?, last_at = ?, presence = ? WHERE id = ?", (name, c["last_at"], presence, c["id"]))
                continue
            try:
                since, wrote = waiting(thread(self.binary, c["id"]), name)  # the messages and their text go with the call
            except (RuntimeError, ValueError, KeyError, TypeError):
                with self.conn:  # this card only: it keeps what it had, or has nothing, and is marked
                    self.conn.execute(
                        """INSERT INTO raincli_cards (id, name, last_at, last_seq, presence, waiting_since, wrote, failed)
                           VALUES (?, ?, ?, -1, ?, NULL, 0, 1)
                           ON CONFLICT (id) DO UPDATE SET name = excluded.name, last_at = excluded.last_at,
                               presence = excluded.presence, failed = 1""", (c["id"], name, c["last_at"], presence))
                continue
            with self.conn:
                self.conn.execute(
                    """INSERT INTO raincli_cards (id, name, last_at, last_seq, presence, waiting_since, wrote, failed)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                       ON CONFLICT (id) DO UPDATE SET name = excluded.name, last_at = excluded.last_at, last_seq = excluded.last_seq,
                           presence = excluded.presence, waiting_since = excluded.waiting_since, wrote = excluded.wrote, failed = 0""",
                    (c["id"], name, c["last_at"], c["last_seq"], presence, since, int(wrote)))


def source_ok(conn):
    r = conn.execute("SELECT state FROM source_health WHERE source = 'raincli'").fetchone()
    return r is None or r["state"] == "ok"  # no pass has ended yet: the cards written so far stand


def cards(conn):
    """For /api/state: whether the last read worked, and one entry per active conversation, newest message first: its id,
    name, when its last message started waiting on us (null when it is not), whether the person ever wrote, and whether
    its thread has never had a good read (last_seq still -1)."""
    out = [{"id": r["id"], "name": r["name"], "waiting_since": r["waiting_since"], "wrote": bool(r["wrote"]), "unread": r["last_seq"] == -1}
           for r in conn.execute("SELECT id, name, waiting_since, wrote, last_seq FROM raincli_cards ORDER BY last_at DESC, name")]
    last = conn.execute("SELECT read_at FROM raincli_inbox").fetchone()
    groups = [{"name": g["name"], "members": g["members"], "taking_part": bool(g["taking_part"])}
              for g in conn.execute("SELECT * FROM raincli_groups ORDER BY name")]
    return {"tried": conn.execute("SELECT 1 FROM source_health WHERE source = 'raincli'").fetchone() is not None, "read": last is not None or bool(out), "read_at": last["read_at"] if last else None, "ok": source_ok(conn), "contacts": out, "groups": groups}


def inbox(conn):
    r = conn.execute("SELECT * FROM raincli_inbox").fetchone()
    return None if r is None else {"mode": r["mode"], "herdr_agent": r["herdr_agent"], "escalation_target": r["escalation_target"],
                                   "held": json.loads(r["held"] or "{}")}


def page(conn, binary, cid):
    """The thread page's data, read live and returned to the one request: nothing is stored. None when the id is not an
    active conversation Atlas knows. {"timed_out": true} when the read passed its timeout, {"unreachable": true} when it failed."""
    card = conn.execute("SELECT name, presence FROM raincli_cards WHERE id = ?", (cid,)).fetchone()
    if card is None:
        return None
    try:
        ms = thread(binary, cid)
    except TimedOut:
        return {"timed_out": True}
    except (RuntimeError, ValueError, KeyError, TypeError):
        return {"unreachable": True}
    out = []
    for m in sorted(ms, key=lambda m: m.get("seq") or 0, reverse=True):
        mine = ours(m, card["name"])
        out.append({"seq": m.get("seq"), "who": "Us" if mine else card["name"], "ours": mine, "at": m.get("created_at"),
                    "reply": bool(m.get("in_reply_to")), "state": m.get("delivery_state"), "reason": m.get("hold_reason"),
                    "text": mask(m["body"]) if isinstance(m.get("body"), str) else "",
                    "files": [{"name": mask(a.get("filename")), "size": a.get("size")} for a in m.get("attachments") or [] if isinstance(a, dict)]})
    ours_held = {}
    for m in out:
        if m["ours"] and m["state"] == "held":
            ours_held[m["reason"] or "held"] = ours_held.get(m["reason"] or "held", 0) + 1
    return {"name": card["name"], "presence": card["presence"], "messages": out, "held": ours_held, "inbox": inbox(conn)}
