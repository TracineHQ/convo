-- Store each message's 1-indexed position within its session.
--
-- Position is the number `inspect` prints and `--from-message/--to-message`
-- take: ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY seq, timestamp, id).
-- Search used to compute it at query time, which costs a scan of every hit's
-- session. Intake now writes it (convo.intake.positions) in the same
-- transaction as the messages.
--
-- A side table instead of a `messages.position` column: backfilling a column
-- takes an UPDATE of every message, which rewrites every row's raw_json page
-- (since 0005 narrowed `messages_au` to `content` it no longer re-indexes FTS
-- as well, but it still rewrites the whole table). This INSERT touches only
-- the new table: no trigger fires and `messages` is not written. Rows go in sorted by message_id so the WITHOUT ROWID b-tree is
-- built append-only. The FK cascade drops a position with its message.

CREATE TABLE message_positions (
    message_id  TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL
) STRICT, WITHOUT ROWID;

INSERT INTO message_positions(message_id, position)
SELECT id, position FROM (
    SELECT id, ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY seq, timestamp, id) AS position
    FROM messages
)
ORDER BY id;
