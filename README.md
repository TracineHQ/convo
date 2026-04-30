# convo

Backup and restore for the convo SQLite database. v0.1.0 ships the storage
layer only; the JSONL intake pipeline and query/analytics commands land in a
later release (see Roadmap).

## Install

PyPI publish is deferred until the intake pipeline lands. For now:

```bash
uv tool install git+https://github.com/<your-org>/convo
# or, from a local clone:
uv tool install /path/to/convo
```

Verify with `convo --help`.

## Available commands

- `convo backup <dest>` -- snapshot the database to an explicit path (`VACUUM INTO`)
- `convo backup --auto` -- timestamped snapshot to the snapshot directory
- `convo restore <src>` -- atomic-swap restore from a snapshot file (snapshot is preserved)

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

Snapshot files are written with the process umask (typically `0644`). On a
shared host where convo data may include sensitive prompt/response content,
set a tighter umask in the cron line:

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

- JSONL intake pipeline that indexes every Claude Code session JSONL from
  `~/.claude/projects/` into the convo database.
- `convo search` -- substring / FTS search over tool calls and messages
- `convo stats` -- tools, commands, sessions, files, skills, model, hooks
- `convo summary` -- one-shot dashboard across sessions, tools, dangers,
  anti-patterns
- `convo diff` -- compare current period vs previous (default 7d)
- `convo inspect` -- session timeline and subagent tree view
- `convo snapshots` / `convo restore --latest` -- snapshot listing and
  latest-snapshot shorthand

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
