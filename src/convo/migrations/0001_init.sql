PRAGMA foreign_keys = ON;

-- Source files indexed into the DB. Two kinds in v0.1:
--   - 'transcript'      Claude Code's own session JSONLs at ~/.claude/projects/*.jsonl
--   - 'guard_decisions' guard plugin's append log at ~/.claude/guard-decisions.jsonl
CREATE TABLE source_files (
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

CREATE INDEX idx_source_files_path ON source_files(path);
CREATE INDEX idx_source_files_kind ON source_files(kind);

-- A Claude Code session. One per JSONL file (1:1 with source_files for now).
CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,
    source_file_id  INTEGER NOT NULL REFERENCES source_files(id) ON DELETE CASCADE,
    project_path    TEXT,
    started_at      TEXT,
    ended_at        TEXT,
    model           TEXT,
    git_branch      TEXT,
    git_commit      TEXT
) STRICT;

CREATE INDEX idx_sessions_source ON sessions(source_file_id);
CREATE INDEX idx_sessions_started ON sessions(started_at);

-- A single turn in a session: user input or assistant response (or system).
CREATE TABLE messages (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    parent_id       TEXT REFERENCES messages(id) ON DELETE SET NULL,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    seq             INTEGER NOT NULL,
    timestamp       TEXT,
    content         TEXT,
    has_newlines    INTEGER NOT NULL DEFAULT 0,
    raw_json        TEXT NOT NULL
) STRICT;

CREATE INDEX idx_messages_session ON messages(session_id, seq);
CREATE INDEX idx_messages_timestamp ON messages(timestamp);
CREATE INDEX idx_messages_multiline ON messages(session_id) WHERE has_newlines = 1;

-- A tool invocation made by the assistant.
CREATE TABLE tool_calls (
    id              TEXT PRIMARY KEY,
    message_id      TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    session_id      TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    name            TEXT NOT NULL,
    input_json      TEXT NOT NULL,
    started_at      TEXT,
    ended_at        TEXT,
    duration_ms     INTEGER,
    has_newlines    INTEGER NOT NULL DEFAULT 0
) STRICT;

CREATE INDEX idx_tool_calls_session ON tool_calls(session_id);
CREATE INDEX idx_tool_calls_message ON tool_calls(message_id, seq);
CREATE INDEX idx_tool_calls_name ON tool_calls(name);
CREATE INDEX idx_tool_calls_name_session ON tool_calls(name, session_id);
CREATE INDEX idx_tool_calls_multiline ON tool_calls(name) WHERE has_newlines = 1;

-- The result returned for a tool call. 1:1 with tool_calls.
CREATE TABLE tool_results (
    tool_call_id    TEXT PRIMARY KEY REFERENCES tool_calls(id) ON DELETE CASCADE,
    message_id      TEXT REFERENCES messages(id) ON DELETE SET NULL,
    is_error        INTEGER NOT NULL DEFAULT 0,
    output_text     TEXT
) STRICT;

-- Schema metadata table.
CREATE TABLE schema_migrations (
    version         INTEGER PRIMARY KEY,
    filename        TEXT NOT NULL,
    applied_at      TEXT NOT NULL
) STRICT;

-- FTS5 virtual tables (content-table mode, trigram tokenizer).
CREATE VIRTUAL TABLE tool_calls_fts USING fts5(
    name,
    input_json,
    content='tool_calls',
    content_rowid='rowid',
    tokenize='trigram'
);

CREATE VIRTUAL TABLE tool_results_fts USING fts5(
    output_text,
    content='tool_results',
    content_rowid='rowid',
    tokenize='trigram'
);

CREATE VIRTUAL TABLE messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='rowid',
    tokenize='trigram'
);

-- Triggers (3 per FTS table = 9 total).

-- tool_calls
CREATE TRIGGER tool_calls_ai AFTER INSERT ON tool_calls BEGIN
    INSERT INTO tool_calls_fts(rowid, name, input_json)
    VALUES (new.rowid, new.name, new.input_json);
END;

CREATE TRIGGER tool_calls_ad AFTER DELETE ON tool_calls BEGIN
    INSERT INTO tool_calls_fts(tool_calls_fts, rowid, name, input_json)
    VALUES ('delete', old.rowid, old.name, old.input_json);
END;

CREATE TRIGGER tool_calls_au AFTER UPDATE ON tool_calls BEGIN
    INSERT INTO tool_calls_fts(tool_calls_fts, rowid, name, input_json)
    VALUES ('delete', old.rowid, old.name, old.input_json);
    INSERT INTO tool_calls_fts(rowid, name, input_json)
    VALUES (new.rowid, new.name, new.input_json);
END;

-- tool_results
CREATE TRIGGER tool_results_ai AFTER INSERT ON tool_results BEGIN
    INSERT INTO tool_results_fts(rowid, output_text)
    VALUES (new.rowid, new.output_text);
END;

CREATE TRIGGER tool_results_ad AFTER DELETE ON tool_results BEGIN
    INSERT INTO tool_results_fts(tool_results_fts, rowid, output_text)
    VALUES ('delete', old.rowid, old.output_text);
END;

CREATE TRIGGER tool_results_au AFTER UPDATE ON tool_results BEGIN
    INSERT INTO tool_results_fts(tool_results_fts, rowid, output_text)
    VALUES ('delete', old.rowid, old.output_text);
    INSERT INTO tool_results_fts(rowid, output_text)
    VALUES (new.rowid, new.output_text);
END;

-- messages
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content)
    VALUES (new.rowid, new.content);
END;

CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
END;

CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
    INSERT INTO messages_fts(rowid, content)
    VALUES (new.rowid, new.content);
END;
