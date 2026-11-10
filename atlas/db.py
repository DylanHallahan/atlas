"""The deploy's SQLite database. On start it is created at VERSION or migrated to it; a restart keeps every row.
The index tables are rebuilt from the sources by a re-index; the kept tables hold what no source can give back."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

# The latest schema of the index tables, which a re-index drops and creates again.
INDEX = ("agents", "screens", "session_names", "messages", "source_health", "team_status", "reports", "items", "item_lines", "item_links",
         "raincli_cards", "raincli_inbox", "raincli_groups")
INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    pane TEXT,
    tab TEXT,
    workspace TEXT,
    runtime TEXT,
    status TEXT,
    session_id TEXT,
    state_since TEXT,
    state_since_exact INTEGER,
    cwd TEXT,
    terminal_title TEXT,
    last_seen TEXT,
    snapshot INTEGER,  -- the last successful herdr snapshot that held this agent
    placement_rule TEXT,
    placed_by TEXT,  -- rule 1: the herdr name whose message first reached this agent's session
    placed_by_key TEXT,  -- that message's key
    placed_session TEXT,  -- the session placed_by was found for
    rebuilt INTEGER,  -- known only from transcripts after a wipe: last_seen is its last transcript event
    name_lost INTEGER,  -- rebuilt and no source names its session: name is runtime and short session id
    now TEXT,  -- a Claude agent's latest step in its own words, from its transcript; never for Codex
    now_at TEXT,
    account TEXT,  -- a Claude agent's account: the store holding its session (work, personal or other); never for Codex
    model TEXT  -- the model of the latest assistant reply in that transcript, as written
);

CREATE TABLE IF NOT EXISTS screens (
    pane TEXT PRIMARY KEY,
    herdr_name TEXT,
    text TEXT,
    read_at TEXT
);

-- Observations the sources forget. herdr rows carry name, pane and session; registry rows carry
-- the registered messaging name, its socket and the time the registry says the name was taken.
CREATE TABLE IF NOT EXISTS session_names (
    session_id TEXT NOT NULL,
    herdr_name TEXT,
    pane TEXT,
    first_seen TEXT,
    last_seen TEXT,
    registry_name TEXT,
    socket TEXT,
    name_since TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS session_names_herdr
    ON session_names (session_id, herdr_name, pane) WHERE herdr_name IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS session_names_registry
    ON session_names (session_id, registry_name, socket) WHERE registry_name IS NOT NULL;

-- One row per message in the last 25 hours, rewritten as a diff after each transcript poll in which every store
-- was ok. Only first lines are stored; full texts stay in memory. Times are UTC with milliseconds.
CREATE TABLE IF NOT EXISTS messages (
    key TEXT PRIMARY KEY,  -- send:<session>:<tool use id>, recv:<session>:<uuid>:<block>, prompt:<session>:<event>
    time TEXT NOT NULL,
    kind TEXT NOT NULL,  -- send, receive_only, prompt
    from_session TEXT,
    from_name TEXT,
    from_label TEXT,
    from_raw TEXT,
    to_session TEXT,
    to_name TEXT,
    to_label TEXT,
    to_raw TEXT,
    first_line TEXT,
    delivered INTEGER,
    ambiguous INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_time ON messages (time);

CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY,
    state TEXT,
    last_ok_at TEXT,
    failing_since TEXT,
    last_error TEXT,
    files_failing INTEGER,
    snapshot INTEGER,  -- herdr only: sequence number of the last successful snapshot
    skipped INTEGER  -- decisions files: entries whose leading stamp did not read cleanly; reports: files over 1 MB
);

-- Each team's STATUS three lines as written, read again when the file's modified time changes.
CREATE TABLE IF NOT EXISTS team_status (
    team TEXT PRIMARY KEY,
    path TEXT,  -- as structure.toml spells it
    modified TEXT,
    working TEXT,
    shipped TEXT,
    waiting TEXT,
    read_at TEXT
);
"""

# Reports, the index table schema 6 added.
REPORTS_SCHEMA = """-- One row per report read: a *.md under a team's reports folder changed since the For Dylan rule's start, or a file
-- its STATUS links. Labels are JSON objects keyed by label, as written; a label not written is absent.
CREATE TABLE IF NOT EXISTS reports (
    path TEXT PRIMARY KEY,  -- absolute, as the team's folder is spelt with ~ expanded
    team TEXT NOT NULL,
    name TEXT NOT NULL,  -- relative to the reports folder (or, for a linked file outside it, its team folder), no .md
    in_reports INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    modified TEXT,
    title TEXT,
    written_at TEXT,  -- UTC with a time from the header; the date as written from frontmatter or the file name
    written_src TEXT,  -- header, frontmatter, filename or none
    author TEXT,
    has_change INTEGER NOT NULL,
    has_ask INTEGER NOT NULL,
    change_labels TEXT,
    ask_labels TEXT,
    asked_at TEXT,
    pr TEXT,
    linked INTEGER NOT NULL DEFAULT 0,  -- the team's STATUS links it now
    status_line TEXT  -- the STATUS line that links it, as written
);
CREATE INDEX IF NOT EXISTS reports_team ON reports (team, name);
"""
INDEX_SCHEMA += REPORTS_SCHEMA

# Work items, the index tables schema 12 added. All rebuilt from the items folders and the briefs and reports.
ITEMS_SCHEMA = """-- One row per item file in a team's items folder; id is <team>:<slug>. updated is UTC. parse_error is set when the
-- file cannot be read as an item. parent, waiting_on_item and nesting are checked when read, not stored.
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    team TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT,
    owner TEXT,
    status TEXT,
    next TEXT,
    waiting_on TEXT,
    waiting_on_item TEXT,
    waiting_default TEXT,
    parent TEXT,
    updated TEXT,
    closed_note TEXT,
    theirs INTEGER NOT NULL DEFAULT 0,
    refs TEXT,  -- the frontmatter links, a JSON list
    proof TEXT,
    stands TEXT,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    parse_error TEXT
);
-- The dated lines of an item's body, in file order.
CREATE TABLE IF NOT EXISTS item_lines (
    item TEXT NOT NULL,
    seq INTEGER NOT NULL,
    at TEXT,
    agent TEXT,
    text TEXT,
    PRIMARY KEY (item, seq)
);
-- A brief, report, ask or pull request that names an item on its Item: line. at is the file's written time.
CREATE TABLE IF NOT EXISTS item_links (
    item TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    at TEXT,
    team TEXT NOT NULL,
    source_path TEXT NOT NULL,
    PRIMARY KEY (item, kind, ref)
);
CREATE INDEX IF NOT EXISTS item_links_source ON item_links (source_path);
"""
INDEX_SCHEMA += ITEMS_SCHEMA

# RainCLI, the index tables schema 13 added: what the org chart's cards need and nothing a person wrote. Schema 16 cut the
# cards to who is waiting on our reply. waiting_since is the time of their first message after our last, when the last
# message is theirs; wrote is whether the person ever wrote.
CARDS_SCHEMA = """CREATE TABLE IF NOT EXISTS raincli_cards (
    id TEXT PRIMARY KEY,  -- the conversation id
    name TEXT NOT NULL,  -- the peer's display name
    last_at TEXT,
    last_seq INTEGER,
    presence TEXT,  -- the thread page shows it
    waiting_since TEXT,
    wrote INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0  -- its last thread read failed
);
"""
RAINCLI_SCHEMA = CARDS_SCHEMA + """-- One row: the inbox connection, as the foot of a thread shows it.
CREATE TABLE IF NOT EXISTS raincli_inbox (
    one INTEGER PRIMARY KEY CHECK (one = 1),
    mode TEXT,
    herdr_agent TEXT,
    escalation_target TEXT,
    held TEXT,  -- a JSON object of hold_reason to count
    read_at TEXT
);
-- Our groups: name, member count and whether we take part.
CREATE TABLE IF NOT EXISTS raincli_groups (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    members INTEGER NOT NULL,
    taking_part INTEGER NOT NULL
);
"""
INDEX_SCHEMA += RAINCLI_SCHEMA

# The kept tables: no source can rebuild them, so a re-index never drops them. This is their text as migration 4 made
# them; later migrations change them, and a new database runs those too.
KEPT_SCHEMA = """
-- What Atlas saw change in the teams' STATUS and decisions files. kind is working, shipped or waiting (a new value
-- of that line, NULL when the line was cleared), entry (a new top-level STATUS entry) or decision. first_seen is when
-- Atlas saw it, or for what existed at the first read of a file, the entry's own written time; NULL there means
-- baseline, never new.
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    team TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT,
    first_seen TEXT,
    written_at TEXT  -- the entry's leading stamp in UTC, when it reads cleanly
);
CREATE UNIQUE INDEX IF NOT EXISTS observations_entry
    ON observations (team, kind, value) WHERE kind IN ('entry', 'decision');
CREATE INDEX IF NOT EXISTS observations_seen ON observations (first_seen);

-- Each STATUS or decisions file Atlas has read, by team, kind and path: what a file holds at its first read is the
-- baseline, whether or not anything in it parsed, and a new path gets its own.
CREATE TABLE IF NOT EXISTS first_reads (
    team TEXT NOT NULL,
    kind TEXT NOT NULL,  -- status or decisions
    path TEXT NOT NULL,  -- as structure.toml spells it
    at TEXT NOT NULL,
    PRIMARY KEY (team, kind, path)
);

-- Each Tailscale login's last look at the briefing, shared by its devices.
CREATE TABLE IF NOT EXISTS last_look (
    login TEXT PRIMARY KEY,
    at TEXT NOT NULL
);
"""


def now(ago=timedelta()):
    """UTC, to the second, in a form that sorts as text."""
    return (datetime.now(UTC) - ago).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path):
    # Each connection is used by one thread; the pollers' are opened in the main thread and handed over.
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def read(conn):
    """One read snapshot for several SELECTs, so values compared across tables come from the same commit."""
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.rollback()


# PRAGMA user_version holds the schema version. Version 1 is the skeleton at db27130, which set none.
# Each migration takes the database from the version before it to its own; it only adds or renames.
MIGRATIONS = {
    2: "ALTER TABLE agents RENAME COLUMN kind TO runtime;",
    3: """
ALTER TABLE agents ADD COLUMN placed_by TEXT;
ALTER TABLE agents ADD COLUMN placed_by_key TEXT;
ALTER TABLE agents ADD COLUMN placed_session TEXT;
ALTER TABLE agents ADD COLUMN rebuilt INTEGER;
ALTER TABLE agents ADD COLUMN name_lost INTEGER;
ALTER TABLE session_names ADD COLUMN registry_name TEXT;
ALTER TABLE session_names ADD COLUMN socket TEXT;
ALTER TABLE session_names ADD COLUMN name_since TEXT;
CREATE UNIQUE INDEX session_names_registry
    ON session_names (session_id, registry_name, socket) WHERE registry_name IS NOT NULL;
CREATE TABLE messages (
    key TEXT PRIMARY KEY,
    time TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_session TEXT,
    from_name TEXT,
    from_label TEXT,
    from_raw TEXT,
    to_session TEXT,
    to_name TEXT,
    to_label TEXT,
    to_raw TEXT,
    first_line TEXT,
    delivered INTEGER,
    ambiguous INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX messages_time ON messages (time);
""",
    4: """
ALTER TABLE agents ADD COLUMN now TEXT;
ALTER TABLE agents ADD COLUMN now_at TEXT;
ALTER TABLE source_health ADD COLUMN skipped INTEGER;
CREATE TABLE team_status (
    team TEXT PRIMARY KEY,
    path TEXT,
    modified TEXT,
    working TEXT,
    shipped TEXT,
    waiting TEXT,
    read_at TEXT
);
""" + KEPT_SCHEMA,
    5: """
-- A decision is its team, its written time and its order among that team's entries with that written time (seq),
-- not its text: an edit updates its row and first_seen stays.
ALTER TABLE observations ADD COLUMN seq INTEGER;  -- decisions only
DROP INDEX observations_entry;  -- keyed decisions by text; dropped first so an edit's text can move to its first row
-- In each (team, written_at) group, the rows recorded in the same read as its first row are different decisions, in
-- file order; a row recorded in a later read is an edit.
CREATE TEMP TABLE heads AS
    SELECT o.team, o.written_at, o.first_seen FROM observations o
    WHERE o.kind = 'decision' AND o.id = (SELECT min(id) FROM observations
                                          WHERE kind = 'decision' AND team = o.team AND written_at IS o.written_at);
UPDATE observations SET seq = (
    SELECT count(*) FROM observations o JOIN temp.heads h
        ON h.team = o.team AND h.written_at IS o.written_at AND o.first_seen IS h.first_seen
    WHERE o.kind = 'decision' AND o.team = observations.team AND o.written_at IS observations.written_at
        AND o.id < observations.id)
WHERE kind = 'decision' AND first_seen IS (SELECT first_seen FROM temp.heads h
                                           WHERE h.team = observations.team AND h.written_at IS observations.written_at);
-- An edited decision keeps its first row and first_seen, with the newest text.
UPDATE observations SET value = (
    SELECT value FROM observations o WHERE o.kind = 'decision' AND o.team = observations.team
        AND o.written_at IS observations.written_at ORDER BY o.id DESC LIMIT 1)
WHERE kind = 'decision' AND seq = 0 AND (SELECT count(*) FROM observations o WHERE o.kind = 'decision'
    AND o.team = observations.team AND o.written_at IS observations.written_at AND o.seq IS NOT NULL) = 1;
DELETE FROM observations WHERE kind = 'decision' AND seq IS NULL;
DROP TABLE temp.heads;
CREATE UNIQUE INDEX observations_entry ON observations (team, kind, value) WHERE kind = 'entry';
CREATE UNIQUE INDEX observations_decision ON observations (team, written_at, seq) WHERE kind = 'decision';
""",
    6: REPORTS_SCHEMA,
    7: """
-- Kept: every POST /api/send, one row each, never the text. login and device are as resolved (null when anonymous);
-- agent and pane are set once the agent is checked live; length is in characters, for a line. outcome is sent, failed,
-- refused:<reason>, or sending while herdr runs (left so only if the process stopped mid-send).
CREATE TABLE sends (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    login TEXT,
    device TEXT,
    device_id TEXT,
    agent TEXT,
    pane TEXT,
    kind TEXT,
    key TEXT,
    length INTEGER,
    outcome TEXT NOT NULL
);
""",
    8: """
-- Placement rule 1 changed (a brief names the parent; a message counts only in the sender's workspace). The stored
-- decisions are an index cache: clear them once, so every live session is placed again under the new rule, as a wipe
-- would place it.
UPDATE agents SET placement_rule = NULL, placed_by = NULL, placed_by_key = NULL, placed_session = NULL;
""",
    9: """
-- An answer from an ask page: the letter picked (A to Z) or the word other, never the note or his own words.
ALTER TABLE sends ADD COLUMN pick TEXT;
""",
    10: """
-- The decisions parser reads more stamps (a weekday or a date in the UTC bracket), so a team's entries at one written
-- time can number differently: each team with stored decisions is re-keyed once, on its next read (status.py).
CREATE TABLE decision_rekey (team TEXT PRIMARY KEY);
INSERT INTO decision_rekey SELECT DISTINCT team FROM observations WHERE kind = 'decision';
""",
    11: """
-- Kept: the changes Dylan has marked reviewed, per login, change and head id. pr is the change's number, or
-- b-<first 7 of its head> when no report names a number.
CREATE TABLE review_marks (
    login TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr TEXT NOT NULL,
    head TEXT NOT NULL,
    at TEXT NOT NULL,
    PRIMARY KEY (login, repo, pr, head)
);
""",
    12: ITEMS_SCHEMA,
    13: RAINCLI_SCHEMA,
    14: """
ALTER TABLE agents ADD COLUMN account TEXT;
ALTER TABLE agents ADD COLUMN model TEXT;
""",
    15: """
-- Kept: every POST /api/upload, one row each, refusals included, never the image. path is the stored file's full path
-- (null when refused); outcome is stored or refused:<reason>; send_id is the sends row whose line named the file.
CREATE TABLE uploads (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    login TEXT,
    device TEXT,
    device_id TEXT,
    ask TEXT,
    path TEXT,
    size INTEGER,
    sha256 TEXT,
    outcome TEXT NOT NULL,
    send_id INTEGER
);
CREATE INDEX uploads_ask ON uploads (ask, send_id);
""",
    16: """
-- The RainCLI cards keep who is waiting on our reply, not counts, times or held reasons. Index data: dropped here, and the
-- next poll fills it from RainCLI.
DROP TABLE raincli_cards;
""" + CARDS_SCHEMA,
    17: """
-- Kept: a send from the page carries a key the page made; a repeat of a key is refused as already sent. Null on every
-- earlier row and on any row that never reached herdr.
ALTER TABLE sends ADD COLUMN client_key TEXT;
CREATE UNIQUE INDEX sends_client_key ON sends (client_key) WHERE client_key IS NOT NULL;
""",
    18: """
-- Index data: the next start rereads every item file, so these fill from the files.
ALTER TABLE items ADD COLUMN proof TEXT;
ALTER TABLE items ADD COLUMN stands TEXT;
""",
}
VERSION = 18
IN_INDEX = {14, 16, 18}  # a new database has these columns from INDEX_SCHEMA already
# A new database: the latest index tables, and the kept tables with every migration since 4.
SCHEMA = INDEX_SCHEMA + KEPT_SCHEMA + "".join(MIGRATIONS[v] for v in range(5, VERSION + 1) if v not in IN_INDEX)


def create(path):
    """Create a new database at VERSION, or apply each missing migration in order, in one transaction.
    True when the index is new: a first start or a re-index, never a restart (no source has been read yet)."""
    conn = sqlite3.connect(path, timeout=10, autocommit=True)  # no implicit COMMIT inside executescript
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("BEGIN IMMEDIATE")
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        if version == 0 and conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'agents'").fetchone():
            version = 1
        if version > VERSION:
            raise SystemExit(f"atlas: database {path} is at version {version}; this code knows up to {VERSION}")
        script = SCHEMA if version == 0 else "".join(MIGRATIONS[v] for v in range(version + 1, VERSION + 1))
        conn.executescript(f"{script}\nPRAGMA user_version = {VERSION};\nCOMMIT;")
        return conn.execute("SELECT 1 FROM source_health LIMIT 1").fetchone() is None
    finally:
        conn.close()  # an error before COMMIT leaves the transaction open, and closing rolls it back


def reindex(path):
    """Drop and create again only the index tables, in one transaction; the kept tables stay.
    Run with the service stopped: its next start rebuilds the index from the sources."""
    create(path)
    conn = sqlite3.connect(path, timeout=10, autocommit=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        drops = "".join(f"DROP TABLE {t};\n" for t in INDEX)
        conn.executescript(f"{drops}{INDEX_SCHEMA}\nCOMMIT;")
    finally:
        conn.close()


# The one agent whose terminal and transcript can carry an outside person's words (RainCLI decision: never stored).
PRIVATE = "raincli-inbox"


def private_sessions(conn):
    """Every session known to have been PRIVATE: the agent row's, and each a herdr observation gave its name."""
    return [r[0] for r in conn.execute(
        """SELECT session_id FROM agents WHERE name = ? AND session_id IS NOT NULL
           UNION SELECT session_id FROM session_names WHERE herdr_name = ?""", (PRIVATE, PRIVATE))]


def scrub(conn):
    """Delete what may already be stored for PRIVATE (its screen, title, now line and message first lines), then truncate the WAL."""
    sessions = private_sessions(conn)
    with conn:
        conn.execute("DELETE FROM screens WHERE herdr_name = ? OR pane IN (SELECT pane FROM agents WHERE name = ?)",
                     (PRIVATE, PRIVATE))
        conn.execute("UPDATE agents SET now = NULL, now_at = NULL, terminal_title = NULL WHERE name = ?", (PRIVATE,))
        conn.execute("""UPDATE messages SET first_line = NULL WHERE first_line IS NOT NULL AND (from_name = ? OR to_name = ?
                        OR from_session IN (SELECT value FROM json_each(?)) OR to_session IN (SELECT value FROM json_each(?)))""",
                     (PRIVATE, PRIVATE, json.dumps(sessions), json.dumps(sessions)))
    # Frames already in the -wal file keep the old text until overwritten; this empties the file.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def set_health(conn, source, at, state="ok", error=None, files_failing=0):
    """Record one read of a source. failing_since is set on the first non-ok read and cleared on the next ok."""
    ok = state == "ok"
    conn.execute(
        """INSERT INTO source_health (source, state, last_ok_at, failing_since, last_error, files_failing)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (source) DO UPDATE SET
               state = excluded.state,
               last_ok_at = coalesce(excluded.last_ok_at, last_ok_at),
               failing_since = CASE WHEN excluded.state = 'ok' THEN NULL
                                    ELSE coalesce(failing_since, excluded.failing_since) END,
               last_error = excluded.last_error,
               files_failing = excluded.files_failing""",
        (source, state, at if ok else None, None if ok else at, error, files_failing),
    )
