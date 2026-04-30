-- Phase 01 minimal stub: only schema_migrations so the migration runner
-- can record itself. Phase 02 (02-schema.md) replaces this with the full
-- schema body (sessions, messages, tool_calls, tool_results, source_files,
-- FTS5 tables, triggers, indexes).
CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,
    filename   TEXT    NOT NULL,
    applied_at TEXT    NOT NULL
) STRICT;
