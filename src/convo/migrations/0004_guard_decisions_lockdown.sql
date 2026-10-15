-- Accept guard's lockdown mode in guard_decisions.mode.
--
-- `guard mode lockdown` logs records with mode "lockdown", which the v2/v3
-- CHECK rejects. SQLite STRICT tables can't ALTER a CHECK constraint, so
-- rebuild via new-table + copy + rename (as 0002 did for source_files), then
-- recreate the indexes, the FTS mirror and its triggers, and repopulate the
-- FTS index from the copied rows.

DROP TRIGGER IF EXISTS guard_decisions_ai;
DROP TRIGGER IF EXISTS guard_decisions_ad;
DROP TRIGGER IF EXISTS guard_decisions_au;
DROP TABLE IF EXISTS guard_decisions_fts;

CREATE TABLE guard_decisions_new (
    id               INTEGER PRIMARY KEY,
    source_file_id   INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    line_no          INTEGER NOT NULL,
    schema_version   INTEGER NOT NULL,
    mode             TEXT NOT NULL CHECK (mode IN ('enforce', 'shadow', 'off', 'lockdown')),
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

INSERT INTO guard_decisions_new SELECT * FROM guard_decisions;
DROP TABLE guard_decisions;
ALTER TABLE guard_decisions_new RENAME TO guard_decisions;

CREATE INDEX idx_guard_decisions_session ON guard_decisions(session_id);
CREATE INDEX idx_guard_decisions_hook    ON guard_decisions(hook_id);
CREATE INDEX idx_guard_decisions_decision ON guard_decisions(decision);
CREATE INDEX idx_guard_decisions_timestamp ON guard_decisions(timestamp);
CREATE INDEX idx_guard_decisions_tool    ON guard_decisions(tool_name);

CREATE VIRTUAL TABLE guard_decisions_fts USING fts5(
    reason,
    command_excerpt,
    content='guard_decisions',
    content_rowid='id',
    tokenize='trigram'
);

INSERT INTO guard_decisions_fts(guard_decisions_fts) VALUES ('rebuild');

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
