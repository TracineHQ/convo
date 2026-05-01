---
name: convo-search
description: Search Claude Code session history (messages, tool calls, tool results) using FTS5. Use when the user asks about past conversations, "find that thing about X", or wants to recall a prior session.
---

# convo-search

Use when the user wants to recall prior Claude Code activity: past messages, a specific tool call, a file edit, an error message, or a conversation about a topic.

## Command

```
convo search "<query>" [--since SPAN] [--project P] [--tool T] [--limit N] --json
```

- Query must be at least 3 characters (FTS5 trigram tokenizer minimum).
- Operators: `+required`, `-excluded` (e.g. `+kafka -test`).
- `--since` accepts `7d`, `24h`, `30d`, etc.
- Always pass `--json` when invoking from a skill.

## Example

```
convo search "kafka migration" --since 30d --limit 10 --json
```

## JSON envelope

```json
{
  "schema_version": 1,
  "search": {
    "query": "...",
    "filters": {"since": "...", "project": "...", "tool": "...", "limit": 10},
    "hits": [
      {"kind": "message|tool_call|tool_result", "id": "...", "session_id": "...",
       "timestamp": "...", "excerpt": "...", "project": "..."}
    ]
  }
}
```

## Notes

- `convo` is on PATH while this plugin is loaded.
- If the DB is empty or missing, suggest `convo index` first, or set `CONVO_DB` to point at an existing index.
