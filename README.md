# convo

Index Claude Code session JSONLs into a local SQLite database, then search,
inspect, snapshot, and analyze the result. v1 covers the intake pipeline,
the storage layer, the read surface (`info`, `search`, `inspect`,
`snapshots`, `restore --latest`), and analytics (`stats`, `summary`,
`diff`).

## Install

convo is intended to ship as a Claude Code plugin/extension. Until that
landing slot exists, install from source:

```bash
uv tool install git+https://github.com/<your-org>/convo
# or, from a local clone:
uv tool install /path/to/convo
```

Verify with `convo --help`.

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
- `convo inspect <session-id> [--json] [--full]` -- session timeline with
  inline tool calls. Accepts a UUID prefix; ambiguous prefixes list
  candidates. `--full` dumps message content verbatim (default truncates to
  200 chars per message).
- `convo snapshots [--json]` -- list snapshot files with `name | size | age`
  columns, newest first.
- `convo backup <dest>` -- snapshot the database to an explicit path (`VACUUM INTO`)
- `convo backup --auto` -- timestamped snapshot to the snapshot directory
- `convo restore <src>` -- atomic-swap restore from a snapshot file (snapshot is preserved)
- `convo restore --latest` -- shorthand for restoring the newest snapshot in
  `$CONVO_BACKUP_DIR`.
- `convo stats <family> [--since SPAN] [--project P] [--json]` -- analytics
  families: `tools`, `commands`, `sessions`, `files`, `model`.
- `convo summary [--since SPAN] [--project P] [--json]` -- composite of all
  five families in one report.
- `convo diff [--since SPAN] [--project P] [--json]` -- current vs previous
  window comparison with deltas. Default span 7d.

Example session:

```bash
convo index
convo info
convo search "kafka" --since 7d --limit 10
convo summary --since 7d
convo inspect <session-id>   # use a prefix from the search hits above
convo snapshots
```

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

- **Windows** is not supported in this version (`os.replace` against an open
  DB raises `PermissionError`). CI matrix is Ubuntu + macOS.
- **Restore is same-filesystem only.** The atomic-replace step writes a
  staging file next to the live DB and then renames it into place, so the
  staging step is always same-FS regardless of where the snapshot source
  lives. The constraint is between the staging file and the live DB
  (always the same parent dir), not between the snapshot source and the
  live DB. Practically: keep `CONVO_DB.parent` writable and you're fine.

## Roadmap

Future releases will add:

- Claude Code plugin/extension packaging (primary distribution target).
- `convo stats hooks` and `convo stats skills` -- deferred to v1.1; both
  require a `0002_live_hooks.sql` schema addition to capture pre/post tool
  hook events and skill invocations from the JSONL.

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
