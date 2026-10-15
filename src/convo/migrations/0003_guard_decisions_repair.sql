-- Repair migration for guard_decisions.
--
-- Some databases reached user_version 2 by applying an EARLIER form of
-- 0002_guard_decisions.sql that only widened source_files.kind, before the
-- guard_decisions table + FTS + triggers were added to that file. Because
-- migrate() never re-runs an already-recorded version, those DBs are stuck at
-- v2 with source_files widened but no guard_decisions table -- `convo
-- index-guard` then fails with "no such table: guard_decisions".
--
-- Re-running 0002 is unsafe (it rebuilds source_files via DROP + RENAME), so
-- this forward migration creates only the guard_decisions objects, idempotently
-- (IF NOT EXISTS), so it is a no-op on healthy DBs that already have them.

CREATE TABLE IF NOT EXISTS guard_decisions (
    id               INTEGER PRIMARY KEY,
    source_file_id   INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    line_no          INTEGER NOT NULL,
    schema_version   INTEGER NOT NULL,
    mode             TEXT NOT NULL CHECK (mode IN ('enforce', 'shadow', 'off')),
    timestamp        TEXT NOT NULL,
    hook_id          TEXT NOT NULL,
    event            TEXT NOT NULL,
    tool_name        TEXT,
    decision         TEXT NOT NULL CHECK (decision IN ('allow', 'deny', 'ask', 'defer', 'pass')),
    reason           TEXT NOT NULL,
    command_excerpt  TEXT,
    session_id       TEXT NOT NULL,
    cwd              TEXT,
    raw_json         TEXT NOT NULL,
    UNIQUE (source_file_id, line_no)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_guard_decisions_session ON guard_decisions(session_id);
CREATE INDEX IF NOT EXISTS idx_guard_decisions_hook    ON guard_decisions(hook_id);
CREATE INDEX IF NOT EXISTS idx_guard_decisions_decision ON guard_decisions(decision);
CREATE INDEX IF NOT EXISTS idx_guard_decisions_timestamp ON guard_decisions(timestamp);
CREATE INDEX IF NOT EXISTS idx_guard_decisions_tool    ON guard_decisions(tool_name);

CREATE VIRTUAL TABLE IF NOT EXISTS guard_decisions_fts USING fts5(
    reason,
    command_excerpt,
    content='guard_decisions',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS guard_decisions_ai AFTER INSERT ON guard_decisions BEGIN
    INSERT INTO guard_decisions_fts(rowid, reason, command_excerpt)
    VALUES (new.id, new.reason, COALESCE(new.command_excerpt, ''));
END;

CREATE TRIGGER IF NOT EXISTS guard_decisions_ad AFTER DELETE ON guard_decisions BEGIN
    INSERT INTO guard_decisions_fts(guard_decisions_fts, rowid, reason, command_excerpt)
    VALUES ('delete', old.id, old.reason, COALESCE(old.command_excerpt, ''));
END;

CREATE TRIGGER IF NOT EXISTS guard_decisions_au AFTER UPDATE ON guard_decisions BEGIN
    INSERT INTO guard_decisions_fts(guard_decisions_fts, rowid, reason, command_excerpt)
    VALUES ('delete', old.id, old.reason, COALESCE(old.command_excerpt, ''));
    INSERT INTO guard_decisions_fts(rowid, reason, command_excerpt)
    VALUES (new.id, new.reason, COALESCE(new.command_excerpt, ''));
END;
