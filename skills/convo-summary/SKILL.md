---
name: convo-summary
description: Get an aggregate summary of Claude Code session activity (tool usage, commands, sessions, files, models). Use when the user asks "what have I been doing", "what tools do I use most", or wants a usage dashboard for a time window.
---

# convo-summary

Use when the user wants a rollup of Claude Code activity: usage stats, a "what did I do this week" dashboard, or a comparison between time windows.

## Command

```
convo summary [--since SPAN] [--project P] --json
```

Alternatives:
- `convo stats <family> --json` for a single family: `tools`, `commands`, `sessions`, `files`, `model`.
- `convo diff --since SPAN --json` for current-vs-previous-window comparison.

## Example

```
convo summary --since 7d --json
```

## JSON envelopes

`convo summary`:
```json
{"schema_version": 1, "summary": {"since": "...", "project": "...",
 "tools": {...}, "commands": {...}, "sessions": {...},
 "files": {...}, "model": {...}}}
```

`convo diff`:
```json
{"schema_version": 1,
 "diff": {"current": {...}, "previous": {...}, "deltas": {...}}}
```

## Notes

- Requires `convo index` to have populated the DB first; an empty DB yields zeroed counts.
- `convo` is on PATH while this plugin is loaded.
