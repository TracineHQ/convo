-- Record how each session was started: the `entrypoint` field Claude Code
-- writes on every transcript line. `cli` is an interactive session; `sdk-cli`
-- (headless `claude -p`), `sdk-py` and `sdk-ts` (the Agent SDKs) are
-- programmatic runs. Read commands leave `sdk-*` sessions out by default.
--
-- NULL means unknown: a transcript written before Claude Code recorded the
-- field. `convo tag-sdk` fills it from transcripts still on disk and can mark
-- a project's templated pipeline runs `sdk-inferred` when the file is gone.
--
-- The backfill reads the value back out of the stored `messages.raw_json`, so
-- it also covers sessions whose transcript Claude Code has since deleted. It
-- takes the first message (by seq) that carries one and looks at no more
-- than a session's first 20 messages: every line of a transcript that has
-- the field carries it, so a session with none in its first 20 predates it,
-- and the bound keeps a large DB from re-reading every message's raw_json.
-- Only `sessions` is written; no trigger fires.

ALTER TABLE sessions ADD COLUMN entrypoint TEXT;

UPDATE sessions SET entrypoint = (
    SELECT ep FROM (
        SELECT m.seq AS seq,
               CASE WHEN json_valid(m.raw_json)
                    THEN json_extract(m.raw_json, '$.entrypoint') END AS ep
        FROM messages m
        WHERE m.session_id = sessions.id AND m.seq < 20
    )
    WHERE typeof(ep) = 'text' AND ep <> ''
    ORDER BY seq
    LIMIT 1
);
