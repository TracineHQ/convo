# convo

Global conversation index and query tool for Claude Code. Indexes JSONL session
files into a single SQLite database with full-text search, then makes it queryable
for tool-call analytics, anti-pattern detection, and session inspection across
every project on your machine.

Status: under construction. v0.1.0 in progress.

## Available commands

- `convo backup <dest>` -- snapshot the database to an explicit path (`VACUUM INTO`)
- `convo backup --auto [--prune --keep N]` -- timestamped snapshot, optionally pruned
- `convo restore <src>` -- atomic-swap restore from a snapshot file
- `convo migrate-legacy [--src ...] [--dest ...] [--dry-run] [--no-keep-legacy] [--json] [--seed N] [--resume-deferred]` --
  port a legacy convo DB (TracineHQ/claude-skills) to the new schema

## Planned commands

- `convo summary` -- one-shot dashboard across sessions, tools, dangers, anti-patterns
- `convo search` -- substring or FTS search over tool calls, with filters and context
- `convo stats {tools, commands, sessions, files, skills, model, hooks}` -- analytics
- `convo diff` -- compare current period vs previous (default 7d)
- `convo inspect` -- session timeline and subagent tree view
- `convo index` -- build / update the index incrementally

## Storage

`convo` indexes every Claude Code session JSONL into a local SQLite database
at `~/.claude/convo.db` (override with `CONVO_DB`). Three storage commands
ship with v0.1.0:

- `convo backup <dest>` -- write a `VACUUM INTO` snapshot to `<dest>`.
- `convo backup --auto [--prune --keep N]` -- write a timestamped snapshot to
  `~/.claude/convo-backups/`, optionally rotating to `N` retained files.
- `convo restore <src>` -- atomically replace the live DB with `<src>`.

Snapshots use microsecond-precision UTC timestamps so concurrent calls cannot
collide on filenames. Restore validates the source before touching the live
DB and explicitly cleans `-wal` / `-shm` sidecars to prevent corruption.

Known limitations: `convo restore` requires the snapshot directory to live on
the same filesystem as the live DB (atomic-replace constraint), and Windows
is not supported in this version (`os.replace` against an open DB raises
`PermissionError`).

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
