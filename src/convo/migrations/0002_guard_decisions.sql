PRAGMA foreign_keys = OFF;

-- Widen source_files.kind to accept guard decision logs.
-- SQLite STRICT tables can't ALTER a CHECK constraint, so rebuild via the
-- standard new-table + copy + rename pattern.
CREATE TABLE source_files_new (
    id              INTEGER PRIMARY KEY,
    path            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL DEFAULT 'transcript'
                    CHECK (kind IN ('transcript', 'guard_decisions')),
    size            INTEGER NOT NULL,
    mtime_ns        INTEGER NOT NULL,
    sha256          TEXT,
    last_indexed_at TEXT NOT NULL,
    message_count   INTEGER NOT NULL DEFAULT 0
) STRICT;

INSERT INTO source_files_new SELECT * FROM source_files;
DROP TABLE source_files;
ALTER TABLE source_files_new RENAME TO source_files;

CREATE INDEX idx_source_files_path ON source_files(path);
CREATE INDEX idx_source_files_kind ON source_files(kind);

-- Guard decision records. One row per JSONL line. Field set mirrors
-- guard's docs/JSONL_FORMAT.md §3 (schema v1).
CREATE TABLE guard_decisions (
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

CREATE INDEX idx_guard_decisions_session ON guard_decisions(session_id);
CREATE INDEX idx_guard_decisions_hook    ON guard_decisions(hook_id);
CREATE INDEX idx_guard_decisions_decision ON guard_decisions(decision);
CREATE INDEX idx_guard_decisions_timestamp ON guard_decisions(timestamp);
CREATE INDEX idx_guard_decisions_tool    ON guard_decisions(tool_name);

-- FTS5 mirror over reason + command_excerpt; trigram tokenizer matches
-- the rest of the schema (see 0001_init.sql).
CREATE VIRTUAL TABLE guard_decisions_fts USING fts5(
    reason,
    command_excerpt,
    content='guard_decisions',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER guard_decisions_ai AFTER INSERT ON guard_decisions BEGIN
    INSERT INTO guard_decisions_fts(rowid, reason, command_excerpt)
    VALUES (new.id, new.reason, COALESCE(new.command_excerpt, ''));
END;

CREATE TRIGGER guard_decisions_ad AFTER DELETE ON guard_decisions BEGIN
    INSERT INTO guard_decisions_fts(guard_decisions_fts, rowid, reason, command_excerpt)
    VALUES ('delete', old.id, old.reason, COALESCE(old.command_excerpt, ''));
END;

CREATE TRIGGER guard_decisions_au AFTER UPDATE ON guard_decisions BEGIN
    INSERT INTO guard_decisions_fts(guard_decisions_fts, rowid, reason, command_excerpt)
    VALUES ('delete', old.id, old.reason, COALESCE(old.command_excerpt, ''));
    INSERT INTO guard_decisions_fts(rowid, reason, command_excerpt)
    VALUES (new.id, new.reason, COALESCE(new.command_excerpt, ''));
END;

PRAGMA foreign_keys = ON;
