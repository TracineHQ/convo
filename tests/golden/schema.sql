-- index idx_guard_decisions_decision
CREATE INDEX idx_guard_decisions_decision ON guard_decisions(decision);

-- index idx_guard_decisions_hook
CREATE INDEX idx_guard_decisions_hook    ON guard_decisions(hook_id);

-- index idx_guard_decisions_session
CREATE INDEX idx_guard_decisions_session ON guard_decisions(session_id);

-- index idx_guard_decisions_timestamp
CREATE INDEX idx_guard_decisions_timestamp ON guard_decisions(timestamp);

-- index idx_guard_decisions_tool
CREATE INDEX idx_guard_decisions_tool    ON guard_decisions(tool_name);

-- index idx_messages_multiline
CREATE INDEX idx_messages_multiline ON messages(session_id) WHERE has_newlines = 1;

-- index idx_messages_session
CREATE INDEX idx_messages_session ON messages(session_id, seq);

-- index idx_messages_timestamp
CREATE INDEX idx_messages_timestamp ON messages(timestamp);

-- index idx_sessions_source
CREATE INDEX idx_sessions_source ON sessions(source_file_id);

-- index idx_sessions_started
CREATE INDEX idx_sessions_started ON sessions(started_at);

-- index idx_source_files_kind
CREATE INDEX idx_source_files_kind ON source_files(kind);

-- index idx_source_files_path
CREATE INDEX idx_source_files_path ON source_files(path);

-- index idx_tool_calls_message
CREATE INDEX idx_tool_calls_message ON tool_calls(message_id, seq);

-- index idx_tool_calls_multiline
CREATE INDEX idx_tool_calls_multiline ON tool_calls(name) WHERE has_newlines = 1;

-- index idx_tool_calls_name
CREATE INDEX idx_tool_calls_name ON tool_calls(name);

-- index idx_tool_calls_name_session
CREATE INDEX idx_tool_calls_name_session ON tool_calls(name, session_id);

-- index idx_tool_calls_session
CREATE INDEX idx_tool_calls_session ON tool_calls(session_id);

-- table guard_decisions
CREATE TABLE "guard_decisions" (
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

-- table guard_decisions_fts
CREATE VIRTUAL TABLE guard_decisions_fts USING fts5(
    reason,
    command_excerpt,
    content='guard_decisions',
    content_rowid='id',
    tokenize='trigram'
);

-- table messages
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

-- table messages_fts
CREATE VIRTUAL TABLE messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='rowid',
    tokenize='trigram'
);

-- table schema_migrations
CREATE TABLE schema_migrations (
    version         INTEGER PRIMARY KEY,
    filename        TEXT NOT NULL,
    applied_at      TEXT NOT NULL
) STRICT;

-- table sessions
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

-- table source_files
CREATE TABLE "source_files" (
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

-- table tool_calls
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

-- table tool_calls_fts
CREATE VIRTUAL TABLE tool_calls_fts USING fts5(
    name,
    input_json,
    content='tool_calls',
    content_rowid='rowid',
    tokenize='trigram'
);

-- table tool_results
CREATE TABLE tool_results (
    tool_call_id    TEXT PRIMARY KEY REFERENCES tool_calls(id) ON DELETE CASCADE,
    message_id      TEXT REFERENCES messages(id) ON DELETE SET NULL,
    is_error        INTEGER NOT NULL DEFAULT 0,
    output_text     TEXT
) STRICT;

-- table tool_results_fts
CREATE VIRTUAL TABLE tool_results_fts USING fts5(
    output_text,
    content='tool_results',
    content_rowid='rowid',
    tokenize='trigram'
);

-- trigger guard_decisions_ad
CREATE TRIGGER guard_decisions_ad AFTER DELETE ON guard_decisions BEGIN
    INSERT INTO guard_decisions_fts(guard_decisions_fts, rowid, reason, command_excerpt)
    VALUES ('delete', old.id, old.reason, COALESCE(old.command_excerpt, ''));
END;

-- trigger guard_decisions_ai
CREATE TRIGGER guard_decisions_ai AFTER INSERT ON guard_decisions BEGIN
    INSERT INTO guard_decisions_fts(rowid, reason, command_excerpt)
    VALUES (new.id, new.reason, COALESCE(new.command_excerpt, ''));
END;

-- trigger guard_decisions_au
CREATE TRIGGER guard_decisions_au AFTER UPDATE ON guard_decisions BEGIN
    INSERT INTO guard_decisions_fts(guard_decisions_fts, rowid, reason, command_excerpt)
    VALUES ('delete', old.id, old.reason, COALESCE(old.command_excerpt, ''));
    INSERT INTO guard_decisions_fts(rowid, reason, command_excerpt)
    VALUES (new.id, new.reason, COALESCE(new.command_excerpt, ''));
END;

-- trigger messages_ad
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
END;

-- trigger messages_ai
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content)
    VALUES (new.rowid, new.content);
END;

-- trigger messages_au
CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
    INSERT INTO messages_fts(rowid, content)
    VALUES (new.rowid, new.content);
END;

-- trigger tool_calls_ad
CREATE TRIGGER tool_calls_ad AFTER DELETE ON tool_calls BEGIN
    INSERT INTO tool_calls_fts(tool_calls_fts, rowid, name, input_json)
    VALUES ('delete', old.rowid, old.name, old.input_json);
END;

-- trigger tool_calls_ai
CREATE TRIGGER tool_calls_ai AFTER INSERT ON tool_calls BEGIN
    INSERT INTO tool_calls_fts(rowid, name, input_json)
    VALUES (new.rowid, new.name, new.input_json);
END;

-- trigger tool_calls_au
CREATE TRIGGER tool_calls_au AFTER UPDATE ON tool_calls BEGIN
    INSERT INTO tool_calls_fts(tool_calls_fts, rowid, name, input_json)
    VALUES ('delete', old.rowid, old.name, old.input_json);
    INSERT INTO tool_calls_fts(rowid, name, input_json)
    VALUES (new.rowid, new.name, new.input_json);
END;

-- trigger tool_results_ad
CREATE TRIGGER tool_results_ad AFTER DELETE ON tool_results BEGIN
    INSERT INTO tool_results_fts(tool_results_fts, rowid, output_text)
    VALUES ('delete', old.rowid, old.output_text);
END;

-- trigger tool_results_ai
CREATE TRIGGER tool_results_ai AFTER INSERT ON tool_results BEGIN
    INSERT INTO tool_results_fts(rowid, output_text)
    VALUES (new.rowid, new.output_text);
END;

-- trigger tool_results_au
CREATE TRIGGER tool_results_au AFTER UPDATE ON tool_results BEGIN
    INSERT INTO tool_results_fts(tool_results_fts, rowid, output_text)
    VALUES ('delete', old.rowid, old.output_text);
    INSERT INTO tool_results_fts(rowid, output_text)
    VALUES (new.rowid, new.output_text);
END;
