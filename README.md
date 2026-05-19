# convo

[![CI](https://img.shields.io/github/actions/workflow/status/TracineHQ/convo/ci.yml?branch=main&label=CI)](https://github.com/TracineHQ/convo/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/TracineHQ/convo/badge)](https://scorecard.dev/viewer/?uri=github.com/TracineHQ/convo)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![PyPI version](https://img.shields.io/pypi/v/tracine-convo)](https://pypi.org/project/tracine-convo/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

> convo is independently maintained and is not affiliated with, endorsed by,
> or sponsored by Anthropic. "Claude" and "Claude Code" are trademarks of
> Anthropic; this project simply reads files Claude Code writes to your
> local filesystem.

Index Claude Code session JSONLs into a local SQLite database, then search,
inspect, snapshot, and analyze the result. Covers the intake pipeline,
the storage layer, the read surface (`info`, `search`, `inspect`,
`snapshots`, `restore --latest`), analytics (`stats`, `summary`, `diff`),
and discovery commands (`projects`, `tools`, `sessions`).

## Install

Three install paths, in priority order. All require Python 3.12+. Verify
with `convo --version` after installing.

### 1. Claude Code plugin (recommended)

Inside Claude Code:

```
/plugin marketplace add TracineHQ/plugins
/plugin install convo@tracine
```

This installs convo from the unified [TracineHQ plugin catalog](https://github.com/TracineHQ/plugins).
The same marketplace also hosts [guard](https://github.com/TracineHQ/guard); once
the marketplace is registered you can install either with one command. See
[What you get when you install the plugin](#what-you-get-when-you-install-the-plugin)
for the full surface.

Standalone alternative (skip the catalog and install convo directly from this repo):

```
/plugin marketplace add TracineHQ/convo
/plugin install convo@tracinehq
```

### 2. PyPI

The PyPI distribution is `tracine-convo`; the installed CLI command is `convo`.

For end-users:

```
pipx install tracine-convo
```

For uv users:

```
uv tool install tracine-convo
```

For one-shot use without installing globally:

```
uvx --from tracine-convo convo --help
```

### 3. From source (fallback)

```
git clone git@github.com:TracineHQ/convo.git
cd convo
uv tool install .
```

Use this path if you want to track `main` directly or hack on convo locally.

## What you get when you install the plugin

Installing convo as a Claude Code plugin (path 1 above) wires up three things
on top of the CLI:

**Auto-index after every session.** A `SessionEnd` hook runs `convo index`
when each Claude Code session ends, so search results stay current without
manual upkeep. Idempotent and fast (sha256-skipped); no-ops gracefully if
`convo` isn't on `PATH`.

**Six slash commands** available inline in Claude Code:

- `/convo:search <query>` — FTS5 search over messages, tool calls, and tool
  results. Default `--limit 20`.
- `/convo:summary [--since SPAN]` — activity dashboard (tools, commands,
  sessions, files, model). Defaults to 7 days.
- `/convo:diff [--since SPAN]` — current vs previous window comparison with
  deltas. Defaults to 7 days.
- `/convo:inspect <session-id-prefix | --latest>` — full message timeline for
  one session.
- `/convo:stats` — tool-call frequency and error rates across all indexed
  sessions.
- `/convo:info` — DB overview (row counts, last index time, top projects,
  snapshots).
- `/convo:projects` — list indexed projects with session counts.
- `/convo:tools` — list tool names with call counts.
- `/convo:sessions` — list recent sessions with timestamps and message counts.

**A `searching-conversation-history` skill** Claude itself can invoke when you
ask history-recall questions like "did I solve this before?", "what was that
fix for X?", or "summarize last week". The skill calls `convo search` /
`convo summary` with `--json`, parses the result, and surfaces matched session
IDs and excerpts back to you.

## Quickstart

Once `convo` is on your `PATH`:

```sh
convo index                          # populate from ~/.claude/projects/
convo info                           # quick overview: row counts, projects, last index
convo search "kafka" --since 7d      # FTS5 over messages, tool calls, tool results
convo summary --since 7d             # tool/command/session/file/model dashboard
convo inspect <session-id>           # use a prefix from the search hits
convo snapshots                      # list backup snapshots
```

`convo info` looks like this on a fresh DB:

```
schema_version   1
db_size          156.0 KiB
last_indexed_at  2026-05-01T04:57:28+00:00

row counts:
  source_files  1
  sessions      1
  messages      5
  tool_calls    1
  tool_results  1

top projects by sessions:
       1  /workspace/example

snapshots:
  dir          ~/.claude/convo-backups
  count        0
  total_bytes  0 B
```

Set `CONVO_DB` to point at a custom DB path; `CONVO_BACKUP_DIR` for snapshot
location; `CLAUDE_PROJECTS_DIR` to override the default `~/.claude/projects/`.

## Available commands

- `convo index [--full] [--projects-dir PATH] [--dry-run] [--json]` --
  walk `~/.claude/projects/<slug>/*.jsonl` and populate the database.
  Idempotent: skips files whose sha256 hasn't changed. `--full` re-indexes
  everything.
- `convo info [--json]` -- schema version, row counts per table, last index
  time, top 5 projects by session count, snapshot directory size.
- `convo search "<query>" [--since SPAN] [--project P] [--tool T] [--limit N] [--json]`
  -- FTS5 search over messages, tool calls, and tool results. `SPAN` accepts
  `7d` / `24h` / `90m` / `30s`. Query supports FTS5 prefixes (`+required`,
  `-excluded`).
- `convo inspect <session-id> | --latest [--json] [--full]` -- session
  timeline with inline tool calls. Accepts a UUID prefix; ambiguous prefixes
  list candidates. `--latest` resolves the most recently started session.
  `--full` dumps message content verbatim (default truncates to 200 chars per
  message).
- `convo snapshots [--json]` -- list snapshot files with `name | size | age`
  columns, newest first.
- `convo backup <dest>` -- snapshot the database to an explicit path (`VACUUM INTO`)
- `convo backup --auto` -- timestamped snapshot to the snapshot directory
- `convo restore <src>` -- atomic-swap restore from a snapshot file (snapshot is preserved)
- `convo restore --latest` -- shorthand for restoring the newest snapshot in
  `$CONVO_BACKUP_DIR`.
- `convo stats <family> [--since SPAN] [--project P] [--json]` -- analytics
  families: `tools`, `commands`, `sessions`, `files`, `model`, `hooks`.
- `convo summary [--since SPAN] [--project P] [--json]` -- composite of all
  six families in one report.
- `convo diff [--since SPAN] [--project P] [--json]` -- current vs previous
  window comparison with deltas. Default span 7d.
- `convo projects [--json]` -- list indexed projects with session counts and
  last-seen timestamps.
- `convo tools [--json]` -- list tool names with total call counts and
  last-seen timestamps.
- `convo sessions [--since SPAN] [--project P] [--limit N] [--json]` -- list
  recent sessions with project, message count, and timestamps.

The JSON envelope shape for every command is documented in
[JSON-ENVELOPE.md](JSON-ENVELOPE.md).

## Storage

The convo database lives at `~/.claude/convo.db` by default. Override with
`CONVO_DB=...` if you want isolation from Claude Code's own state directory
or want to keep multiple databases. Snapshots default to a `convo-backups/`
sibling of the resolved DB; override with `CONVO_BACKUP_DIR=...`.

- `convo backup <dest>` -- write a `VACUUM INTO` snapshot to `<dest>`.
- `convo backup --auto` -- write a timestamped snapshot to
  `<CONVO_DB parent>/convo-backups/`. Snapshots are append-only; manage
  history yourself (e.g. `find ... -mtime +30 -delete`) or run
  `just snapshots-clean` from a contributor checkout.
- `convo restore <src>` -- atomically replace the live DB with a copy of
  `<src>`. The snapshot file is preserved.

Snapshots use microsecond-precision UTC timestamps so concurrent calls cannot
collide on filenames. Restore validates the source before touching the live
DB, copies it to a `<db>.restoring` staging file co-located with the live DB,
then atomically replaces. `-wal` / `-shm` sidecars are unlinked first to
prevent corruption.

Snapshot files are written `0600` (owner read/write only) regardless of the
process umask, since convo data may include prompt/response content. The live
DB at `~/.claude/convo.db` still inherits the process umask — set
`umask 077` in the shell or cron line that creates it if you want owner-only
permissions on the live DB as well:

```cron
0 3 * * * umask 077 && /path/to/convo backup --auto
```

### Known limitations

- **Restore is same-filesystem only.** The atomic-replace step writes a
  staging file next to the live DB and then renames it into place, so the
  staging step is always same-FS regardless of where the snapshot source
  lives. The constraint is between the staging file and the live DB
  (always the same parent dir), not between the snapshot source and the
  live DB. Practically: keep `CONVO_DB.parent` writable and you're fine.

## Roadmap

Future releases will add:

- `convo stats skills` -- deferred to a future release; requires a schema
  addition to capture skill invocations from the JSONL.

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
