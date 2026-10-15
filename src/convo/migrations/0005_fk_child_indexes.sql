-- Index the child side of the two ON DELETE SET NULL foreign keys, and stop
-- non-text updates from rewriting FTS rows.
--
-- messages.parent_id and tool_results.message_id reference messages(id) with
-- ON DELETE SET NULL but had no index, so every deleted message made SQLite
-- scan both tables to find children. Re-indexing a grown transcript deletes
-- all of its rows first, which made the delete quadratic (about 17 minutes for
-- one 20k-record file in a 120k-message DB; 6.6 s with these indexes).
--
-- The *_au FTS triggers fired on an UPDATE of any column, so the SET NULL
-- updates above (and any future backfill of a non-text column) deleted and
-- re-inserted the row's FTS entry. They now fire only when an FTS-indexed
-- column changes.
--
-- Additive only: no table is rebuilt, so this replays inside migrate()'s
-- BEGIN EXCLUSIVE with foreign_keys ON.

CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id);
CREATE INDEX IF NOT EXISTS idx_tool_results_message ON tool_results(message_id);

DROP TRIGGER IF EXISTS messages_au;
CREATE TRIGGER messages_au AFTER UPDATE OF content ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.rowid, old.content);
    INSERT INTO messages_fts(rowid, content)
    VALUES (new.rowid, new.content);
END;

DROP TRIGGER IF EXISTS tool_calls_au;
CREATE TRIGGER tool_calls_au AFTER UPDATE OF name, input_json ON tool_calls BEGIN
    INSERT INTO tool_calls_fts(tool_calls_fts, rowid, name, input_json)
    VALUES ('delete', old.rowid, old.name, old.input_json);
    INSERT INTO tool_calls_fts(rowid, name, input_json)
    VALUES (new.rowid, new.name, new.input_json);
END;

DROP TRIGGER IF EXISTS tool_results_au;
CREATE TRIGGER tool_results_au AFTER UPDATE OF output_text ON tool_results BEGIN
    INSERT INTO tool_results_fts(tool_results_fts, rowid, output_text)
    VALUES ('delete', old.rowid, old.output_text);
    INSERT INTO tool_results_fts(rowid, output_text)
    VALUES (new.rowid, new.output_text);
END;

DROP TRIGGER IF EXISTS guard_decisions_au;
CREATE TRIGGER guard_decisions_au AFTER UPDATE OF reason, command_excerpt ON guard_decisions BEGIN
    INSERT INTO guard_decisions_fts(guard_decisions_fts, rowid, reason, command_excerpt)
    VALUES ('delete', old.id, old.reason, COALESCE(old.command_excerpt, ''));
    INSERT INTO guard_decisions_fts(rowid, reason, command_excerpt)
    VALUES (new.id, new.reason, COALESCE(new.command_excerpt, ''));
END;
