# convo JSON envelope contract

Every command that accepts `--json` (or `--format=json`) emits a single JSON
object on stdout with this shape:

```json
{"schema_version": 2, "<command>": {...}}
```

The top-level key is always the command name. Errors use the `"error"` key:

```json
{"schema_version": 2, "error": {"message": "..."}}
```

**Breaking-change history.** v1 (<=1.1.0) used the same two-key structure for
most commands but the `schema_version` field was 1. All version constants were
bumped to 2 in the v2.0.0 release as part of the agent-experience redesign.
No commands had flat top-level shapes in v1 -- the wrapper key was always
present -- so the only breaking change at the envelope level is the version
number itself and the per-command field changes listed in CHANGELOG.md.


## Version-bump policy

This is informal contract guidance; convo does not yet enforce per-command
semver. The intent:

- **Major** (`schema_version` bump): shape change at the command level --
  fields moved between the wrapper and body, fields removed, semantic change
  to an existing field.
- **Minor** (additive): new fields inside an existing command body, new
  commands added.
- **Patch**: typo fixes or pure documentation changes.

Consumers should guard on `schema_version` and fail loudly on an unexpected
value rather than silently misinterpreting the body.


## Per-command shapes

Abbreviated -- arrays use `[...]` to indicate zero-or-more items of the shown
shape.

### `info`

```json
{
  "schema_version": 2,
  "info": {
    "db_schema_version": 2,
    "row_counts": {
      "source_files": 12,
      "sessions": 42,
      "messages": 1800,
      "tool_calls": 540,
      "tool_results": 537
    },
    "last_indexed_at": "2026-05-18T12:00:00+00:00",
    "top_projects": [
      {"project_path": "/workspace/myapp", "session_count": 18}
    ],
    "db_size_bytes": 2097152,
    "snapshot_dir_path": "/home/user/.claude/convo-backups",
    "snapshot_count": 3,
    "snapshot_total_bytes": 6291456
  }
}
```

`last_indexed_at` is ISO 8601 or `null` when the DB has never been indexed.
`top_projects` contains up to 5 entries ordered by `session_count` descending.

---

### `search`

```json
{
  "schema_version": 2,
  "search": {
    "query": "kafka consumer lag",
    "filters": {
      "since": "7d",
      "project": null,
      "tool": null,
      "session": null,
      "tool_exact": false,
      "limit": 10
    },
    "total": 2,
    "hits": [
      {
        "kind": "message",
        "id": 101,
        "session_id": "sess-id-prefix",
        "timestamp": "2026-05-10T09:14:33.000Z",
        "excerpt": "The [kafka consumer] lag was 8 s.",
        "indices": [[5, 19]],
        "project": "/workspace/myapp",
        "role": "assistant",
        "position": 12
      },
      {
        "kind": "tool_result",
        "id": 202,
        "session_id": "sess-id-prefix",
        "timestamp": "2026-05-10T09:15:01.000Z",
        "excerpt": "offset [lag]: 0",
        "indices": [[8, 11]],
        "project": "/workspace/myapp",
        "tool_origin": "Bash",
        "position": null
      }
    ]
  }
}
```

`hits[].kind` is one of `"message"`, `"tool_call"`, or `"tool_result"`.

`hits[].indices` is a list of `[start, end]` UTF-16 char offsets into
`hits[].excerpt`. The `excerpt` retains `[X]` brackets around hit positions;
`indices` gives `[start, end]` offsets pointing at the content between each
pair of brackets.

`hits[].role` is present for `kind="message"` (`"user"` or `"assistant"`).
`hits[].tool` is present for `kind="tool_call"`. `hits[].tool_origin` is
present for `kind="tool_result"`.

`hits[].position` is the hit's 1-indexed message number within its session,
the same number `inspect` reports as `messages[].position` and accepts in
`--from-message/--to-message`. So
`convo inspect <session_id> --from-message P --to-message P --max-chars 0`
returns the hit's full message. For `kind="tool_call"` it is the message that
made the call; for `kind="tool_result"` it is `null` (inspect does not show
tool output).

When `total=0` a `"suggestions"` key may appear:

```json
{
  "schema_version": 2,
  "search": {
    "query": "kafka-consumer-lag",
    "filters": {...},
    "total": 0,
    "suggestions": [
      {"kind": "hyphen_split", "description": "Try: kafka consumer lag"}
    ]
  }
}
```

---

### `inspect`

```json
{
  "schema_version": 2,
  "inspect": {
    "session": {
      "id": "session-id-prefix-here",
      "started_at": "2026-05-10T09:14:00.000Z",
      "ended_at": "2026-05-10T09:45:00.000Z",
      "project_path": "/workspace/myapp",
      "model": "claude-sonnet-4-6",
      "git_branch": "main"
    },
    "messages": [
      {
        "id": 1,
        "position": 1,
        "role": "user",
        "timestamp": "2026-05-10T09:14:01.000Z",
        "content": "How do I fix the kafka lag?",
        "truncated": false,
        "tool_calls": [
          {"id": "tc_001", "name": "Bash", "input_json": "{...}", "truncated": false, "started_at": "..."}
        ]
      }
    ],
    "truncated": false,
    "total_messages": 24,
    "from_message": null,
    "to_message": null
  }
}
```

`truncated` is `true` when the message list was capped (default cap: 50
messages; `--full` removes the cap). `messages[].truncated` is `true` when
that message's `content` was clipped at the `--max-chars` limit (default 200;
`--max-chars 0` never clips; `--full` has no effect on per-message truncation).
Clipped content ends in `...`. `tool_calls[].input_json` is complete by
default; an explicit `--max-chars N` clips it the same way (so it may no
longer parse as JSON) and sets `tool_calls[].truncated`.

`messages[].position` is the message's 1-indexed number in the session
(messages are ordered by `seq`, then `timestamp`, then `id`); the prose view
prints the same numbers.

`--from-message N` / `--to-message M` restrict `messages` to that 1-indexed,
inclusive range of the session; `from_message` / `to_message` echo them (or
`null`). An end past the last message is clamped; `total_messages` still
counts the whole session. A reversed range or `--from-message` past the last
message returns the error envelope.

`inspect --timeline --json` returns a timeline body under the same key:

```json
{
  "schema_version": 2,
  "inspect": {
    "session": {
      "id": "session-id-prefix-here",
      "started_at": "2026-05-10T09:14:00.000Z",
      "ended_at": "2026-05-10T09:45:00.000Z",
      "project_path": "/workspace/myapp",
      "model": "claude-sonnet-4-6",
      "git_branch": "main"
    },
    "timeline": {
      "duration_seconds": 1860,
      "message_count": 24,
      "tool_call_count": 7,
      "from_message": null,
      "to_message": null,
      "events": [
        {"offset_seconds": 0, "role": "user", "tool": null, "preview": "How do I fix the kafka lag?", "truncated": false, "position": 1},
        {"offset_seconds": 12, "role": "tool_call", "tool": "Bash", "preview": "{...}", "truncated": false, "position": 2}
      ]
    }
  }
}
```

`session` has the same shape as in the normal view. `events[].role` is a
message role or `"tool_call"`; `tool` is set only for `tool_call` events.
Every `preview` (message content or tool-call input JSON) is clipped to
`--max-chars` (default 80, no `...` suffix; `0` never clips) and keeps its
newlines; `truncated` is `true` when it was clipped. `position` is the
message number (as in the normal view); a `tool_call` event carries its
parent message's position. `from_message` /
`to_message` echo the requested range, or `null`.

---

### `summary`

```json
{
  "schema_version": 2,
  "summary": {
    "since": "7d",
    "project": null,
    "tools": {"total": 840, "top_by_frequency": [...], "top_by_median_duration": [...], "error_rates": [...]},
    "commands": {"total": 42, "top_commands": [...]},
    "sessions": {"total": 42, "sessions_with_duration": 40, "median_duration_s": 1260.0, "p95_duration_s": 5400.0, "hour_of_day": [...]},
    "files": {"total": 12, "total_message_count": 1800, "top_files": [...]},
    "model": {"total": 42, "null_count": 2, "by_model": [...]}
  }
}
```

`summary` is the `stats` families inlined under a single envelope. Sub-object
shapes match `stats` exactly (see above). `since` is a span string or `null`
for all-time. Note: `summary` does not include the `hooks` family.

---

### `diff`

```json
{
  "schema_version": 2,
  "diff": {
    "span_seconds": 604800.0,
    "project": null,
    "current": {
      "lower": "2026-05-11T12:00:00.000000Z",
      "upper": "2026-05-18T12:00:00.000000Z",
      "tool_calls_total": 840,
      "tool_calls_by_name": {"Bash": 420},
      "commands_total": 42,
      "commands_top": {"fix the kafka consumer lag": 5},
      "sessions_count": 42,
      "sessions_median_seconds": 1260.0,
      "sessions_p95_seconds": 5400.0,
      "files_count": 12,
      "model_histogram": {"claude-sonnet-4-6": 40}
    },
    "previous": {"lower": "...", "upper": "...", "tool_calls_total": 700, "...": "..."},
    "deltas": {
      "tool_calls_total": {"absolute": 140, "pct": 0.2},
      "tool_calls_by_name": {"Bash": {"absolute": 70, "pct": 0.2}},
      "sessions_median_seconds": {"absolute": 160.0, "pct": 0.1455},
      "...": "..."
    }
  }
}
```

`current` and `previous` have identical shapes. `deltas` mirrors the same
keys; each value is `{"absolute": <float>, "pct": <float|null>}`. `pct` is
`null` when the previous-window value was 0. `span_seconds` is the window
length in seconds.

---

### `stats`

All six families share the same wrapper; `family` names the sub-report. Body
fields vary per family:

| family | extra fields |
|---|---|
| `tools` | `total`, `top_by_frequency`, `top_by_median_duration`, `error_rates` |
| `commands` | `total`, `top_commands` |
| `sessions` | `total`, `sessions_with_duration`, `median_duration_s`, `p95_duration_s`, `hour_of_day` |
| `files` | `total`, `total_message_count`, `top_files` |
| `model` | `total`, `null_count`, `by_model` |
| `hooks` | `total`, `top_by_hook`, `by_decision` |

Example (`tools`):

```json
{
  "schema_version": 2,
  "stats": {
    "family": "tools",
    "total": 840,
    "top_by_frequency": [{"name": "Bash", "count": 420}],
    "top_by_median_duration": [{"name": "Bash", "median_ms": 312.5, "sample_count": 420}],
    "error_rates": [{"name": "Bash", "total": 420, "errors": 12, "error_rate": 0.0286}]
  }
}
```

`hour_of_day` (sessions family) is always a 24-element array; index `h` is
the count of sessions that started at hour `h` UTC. `null_count` (model family)
counts sessions whose model field was not recorded.

---

### `projects`

```json
{
  "schema_version": 2,
  "projects": {
    "items": [
      {
        "path": "/workspace/myapp",
        "sessions": 42,
        "last_seen": "2026-05-18T12:00:00.000Z"
      }
    ]
  }
}
```

`last_seen` is the `started_at` of the most recent session for that project,
or `null` if unavailable.

---

### `tools`

```json
{
  "schema_version": 2,
  "tools": {
    "items": [
      {
        "name": "Bash",
        "calls": 420,
        "last_seen": "2026-05-18T12:00:00.000Z"
      }
    ]
  }
}
```

Ordered by `calls` descending. `last_seen` is the timestamp of the most
recent tool call, or `null` if unavailable.

---

### `sessions`

```json
{
  "schema_version": 2,
  "sessions": {
    "items": [
      {
        "id": "session-id-prefix-here-...",
        "project_path": "/workspace/myapp",
        "started_at": "2026-05-18T09:14:00.000Z",
        "ended_at": "2026-05-18T09:45:00.000Z",
        "message_count": 24
      }
    ]
  }
}
```

Ordered by `started_at` descending. Accepts `--since`, `--project`, and
`--limit`.

---

### `snapshots`

```json
{
  "schema_version": 2,
  "snapshots": {
    "snapshot_dir": "/home/user/.claude/convo-backups",
    "entries": [
      {
        "name": "convo-2026-05-18T120000.000000Z.db",
        "path": "/home/user/.claude/convo-backups/convo-2026-05-18T120000.000000Z.db",
        "timestamp_utc": "2026-05-18T12:00:00+00:00",
        "size_bytes": 2097152,
        "age_human": "1h ago"
      }
    ]
  }
}
```

Entries ordered newest-first.

---

### `backup`

```json
{
  "schema_version": 2,
  "backup": {
    "snapshot_path": "/home/user/.claude/convo-backups/convo-2026-05-18T120000.000000Z.db",
    "size_bytes": 2097152
  }
}
```

---

### `restore`

```json
{
  "schema_version": 2,
  "restore": {
    "source": "/home/user/.claude/convo-backups/convo-2026-05-18T120000.000000Z.db"
  }
}
```

---

### `index`

Standard case (transcript ingest):

```json
{
  "schema_version": 2,
  "index": {
    "status": "success",
    "files_seen": 12,
    "files_indexed": 3,
    "files_skipped": 9,
    "files_failed": 0,
    "rows_inserted": {"messages": 120, "tool_calls": 30, "tool_results": 29},
    "unknown_record_types": {},
    "errors": [],
    "duration_ms": 420
  }
}
```

`status` is `"success"`, `"partial"` (some files indexed, some failed), or
`"error"` (all files failed). Exit code is 1 when `status="error"`.

Guard-log variant (`convo index-guard --json`):

```json
{
  "schema_version": 2,
  "guard": {
    "path": "/home/user/.claude/guard-decisions.jsonl",
    "skipped_reason": null,
    "error": null,
    "inserted_rows": {"guard_decisions": 45},
    "source_file_id": 3
  }
}
```

`skipped_reason` is non-null when the file was sha256-unchanged (no-op).
`error` is non-null when ingest failed.

---

### Error envelope

Any command that encounters a fatal error (bad DB path, ambiguous session
prefix, project not found, etc.) emits:

```json
{
  "schema_version": 2,
  "error": {"message": "no sessions matched prefix 'abc'"}
}
```

Exit code is always 1 for error envelopes.

---

## Reference

Spec that drove the v2 redesign:
`docs/superpowers/specs/2026-05-18-search-ax-design.md` (local, gitignored)
