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

convo is a CLI plus a Claude Code plugin. The plugin's slash commands, skill
and hook all run the `convo` CLI, so install both. Requires Python 3.12+.

1. The CLI, from PyPI (distribution `tracine-convo`, command `convo`):

   ```
   pipx install tracine-convo
   ```

   `uv tool install tracine-convo` works too. Check with `convo --version`.

2. The plugin, inside Claude Code:

   ```
   /plugin marketplace add TracineHQ/plugins
   /plugin install convo@tracine
   ```

   This comes from the [TracineHQ plugin catalog](https://github.com/TracineHQ/plugins),
   which also hosts [guard](https://github.com/TracineHQ/guard) and
   [eval-kit](https://github.com/TracineHQ/eval-kit). See
   [What you get when you install the plugin](#what-you-get-when-you-install-the-plugin).

Then run `convo index` once to build the database. The plugin's hook re-runs
it at the end of every session.

**Other routes.** The CLI works on its own, without the plugin. To install the
plugin from this repo instead of the catalog:
`/plugin marketplace add TracineHQ/convo`, then `/plugin install convo@tracinehq`.
To track `main` or hack on convo: `git clone git@github.com:TracineHQ/convo.git`,
`cd convo`, `uv tool install .`. To try it without installing:
`uvx --from tracine-convo convo --help`.

**SQLite substrate.** convo uses SQLite STRICT tables (3.37+) and FTS5
virtual tables. Stock Python on macOS, Windows, and mainstream Linux distros
(Ubuntu, Debian, Fedora) ships SQLite that satisfies both. Minimal images
(Alpine, some slim Docker bases) sometimes omit FTS5 from the SQLite build.
Check yours with `python -c "import sqlite3; print(sqlite3.sqlite_version);
conn=sqlite3.connect(':memory:'); conn.execute('CREATE VIRTUAL TABLE t USING fts5(x)')"` --
if that runs cleanly, you're good.

## What you get when you install the plugin

The plugin wires up three things on top of the CLI:

**Auto-index after every session, and before each compaction.** A `SessionEnd`
hook runs `convo hook session-end`, which starts a background index of the
session that just ended and returns in milliseconds (Claude Code gives
SessionEnd hooks 1.5 s). The background run's output goes to
`<CONVO_DB>.hook.log`, each line stamped with the time and the session id.
If another convo writer is already running, one background run waits for it
(up to an hour) and then indexes every transcript, so sessions that end in
the meantime still get indexed; their own runs see it waiting and exit.
After an upgrade that adds a migration, the next background run applies it
before indexing; read commands never migrate, and on a DB too old to read
they ask you to run `convo migrate`. Upgrade the CLI along with the plugin:
with an older CLI the `SessionEnd` hook falls back to an in-hook
`convo index`. No-ops gracefully if `convo` isn't on `PATH`.

A session that runs for days never reaches `SessionEnd`, so a `PreCompact`
hook runs `convo hook pre-compact` as well. Just before Claude Code compacts
a session (automatically or with `/compact`) it starts the same kind of
background run, `convo index --session <id> --no-wait`, and returns at once;
compaction does not wait on the index. If another convo writer is running,
that run logs it and exits rather than queueing, since the next compaction or
the session's end indexes whatever accrued. So a long session is as fresh as
its last compaction: run `convo index` when you need it searchable sooner.
With an older CLI the `PreCompact` hook does nothing (it writes an error line
to `~/.claude/convo-hook-errors.log`).

Both hooks cover the session's subagent transcripts too (see
[Subagent sessions](#subagent-sessions)).

**Nine slash commands** available inline in Claude Code:

- `/convo:search <query>` -- FTS5 search over messages, tool calls, and tool
  results. Shows up to 20 hits.
- `/convo:summary [--since SPAN]` -- activity dashboard (tools, commands,
  sessions, files, model). Defaults to 7 days.
- `/convo:diff [--since SPAN]` -- current vs previous window comparison with
  deltas. Defaults to 7 days.
- `/convo:inspect <session-id-prefix | --latest>` -- full message timeline for
  one session.
- `/convo:stats` -- tool-call frequency and error rates across all indexed
  sessions.
- `/convo:info` -- DB overview (row counts, last index time, top projects,
  snapshots).
- `/convo:projects` -- list indexed projects with session counts.
- `/convo:tools` -- list tool names with call counts.
- `/convo:sessions` -- list recent sessions with timestamps and message counts.

**A `searching-conversation-history` skill** Claude itself can invoke when you
ask history-recall questions like "did I solve this before?", "what was that
fix for X?", or "summarize last week". The skill runs `convo search`,
`convo summary` and the discovery commands, and surfaces matched session IDs
and excerpts back to you.

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
schema_version   6
db_size          208.0 KiB
last_indexed_at  2026-10-15T19:19:02.725664+00:00

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

Every command takes the global `--db PATH` before the command name
(`convo --db ./work.db info`). It beats `CONVO_DB`; a relative path resolves
against the current directory.

- `convo index [--full] [--projects-dir PATH] [--session ID] [--dry-run]
  [--json]` -- walk `~/.claude/projects/<slug>/*.jsonl` and each session's
  subagent transcripts, most recently modified first, and populate the
  database. Idempotent: skips files whose sha256 hasn't changed. `--full`
  re-indexes everything; `--session` indexes one session's transcript and its
  subagents'. Applies pending migrations, and waits for any other convo writer
  to finish (`--no-wait` exits instead; `--no-migrate` refuses to migrate).
  `--dry-run` does neither.
- `convo migrate [--backup] [--json]` -- apply pending schema migrations
  now. Read commands never migrate; `convo index` and the hook's background
  run do, so use this to migrate on your own schedule or when a read command
  asks for it. `convo info` shows how many are pending. `--backup` snapshots
  the DB to the snapshot directory first, if anything is pending; since the
  hook's background run usually migrates first, take a `convo backup --auto`
  with your current CLI before upgrading if you want a way back. A migration
  can take minutes on a large DB; reads keep working meanwhile. Run
  `convo index` afterwards to catch up.
- `convo index-guard [--path PATH] [--full] [--json]` -- index
  [guard](https://github.com/TracineHQ/guard)'s decision log for
  `convo stats hooks`. Path: `--path`, then `$GUARD_DECISIONS_PATH`, then
  `~/.claude/guard-decisions.jsonl`. Skips an unchanged log.
- `convo info [--json]` -- schema version, row counts per table, last index
  time, top 5 projects by session count, snapshot directory size.
- `convo search "<query>" [--since SPAN] [--project P] [--session ID]
  [--tool T] [--tool-exact] [--limit N] [--excerpt-chars N] [--fields LIST]
  [--format prose|json]` -- FTS5 search over messages, tool calls, and tool
  results.
  - Terms are ANDed. Quote a `"phrase"`, put `OR` between terms, and prefix a
    term with `-` to exclude it.
  - `SPAN` is `7d` / `24h` / `90m` / `30s` / `2w` / `1y` or an ISO date.
  - `--project` takes an exact path, a basename or a unique substring
    (`convo projects` lists them). `--session` takes a session-id prefix.
    `--tool` is a prefix match unless `--tool-exact` is given.
  - `--limit` defaults to 10. JSON `total` counts every match, before the
    limit.
  - `--excerpt-chars` sets the excerpt width, up to the FTS5 snippet maximum
    of about 64 characters (the default).
  - `--fields` (prose output) prints one tab-separated line per hit with the
    listed fields:
    `session`, `ts`, `when`, `project`, `kind`, `excerpt`, `command`,
    `content`, `output`, `tool`, `position`.
  - Each hit reports its message `position`; pass it to
    `convo inspect <session> --from-message P --to-message P --max-chars 0`
    for the full text.
  - `--json` still works as an alias for `--format json` but is deprecated
    for `search`.
- `convo inspect <session-id> | --latest [--json] [--full] [--timeline]
  [--from-message N] [--to-message M] [--max-chars N]` -- session timeline
  with inline tool calls. Accepts a UUID prefix; ambiguous prefixes list
  candidates. `--latest` resolves the most recently started session. `--full`
  lifts the 50-message cap. `--from-message`/`--to-message` select a 1-indexed
  inclusive range. `--max-chars` sets the per-message content and tool-call
  input limit (defaults 200 and 80, or 80 with `--timeline`); `--max-chars 0`
  shows full text.
- `convo snapshots [--json]` -- list snapshot files with `name | size | age`
  columns, newest first.
- `convo backup <dest>` -- snapshot the database to an explicit path (`VACUUM INTO`)
- `convo backup --auto` -- timestamped snapshot to the snapshot directory
- `convo restore <src>` -- atomic-swap restore from a snapshot file (snapshot is preserved)
- `convo restore --latest` -- shorthand for restoring the newest snapshot in
  `$CONVO_BACKUP_DIR`.
- `convo stats <family> [--since SPAN] [--project P] [--include-subagents] [--json]`
  -- analytics families: `tools`, `commands`, `sessions`, `files`, `model`,
  `hooks`.
- `convo summary [--since SPAN] [--project P] [--include-subagents] [--json]`
  -- composite of the `tools`, `commands`, `sessions`, `files` and `model`
  families in one report. `hooks` is not included; run `convo stats hooks` for
  it.
- `convo diff [--since SPAN] [--project P] [--include-subagents] [--json]` --
  current vs previous window comparison with deltas. Default span 7d.
- `convo projects [--include-subagents] [--format prose|json]` -- list indexed
  projects with session counts and last-seen timestamps.
- `convo tools [--format prose|json]` -- list tool names with total call
  counts and last-seen timestamps.
- `convo sessions [--since SPAN] [--project P] [--limit N] [--include-subagents]
  [--format prose|json]` -- list recent sessions with project, message count,
  and timestamps.

`projects`, `tools` and `sessions` also accept `--json` as an alias for
`--format json`.

### Subagent sessions

Claude Code writes each subagent's transcript beside its parent session, at
`<slug>/<session-id>/subagents/agent-<id>.jsonl` (or under
`subagents/workflows/<workflow>/` for workflow agents), with an
`agent-<id>.meta.json` next to it. convo indexes each one as a session of its
own:

- Its id is `<agent-id>.<parent-session-id>` (a dot joins them), so the link to
  the parent is in the `sessions` table with no extra column. The agent id is
  the `agentId` Claude Code reports, so `convo inspect <agent-id>` finds it.
- Its first message is a `system` message with the agent type and description
  from the meta file (`Subagent (Explore): Trace the dispersed camping
  rules`), so `convo search` can find a subagent by what it was asked to do.
  The meta file is kept whole in that message's `raw_json`. A subagent with no
  usable meta file just has no such message.
- Its messages and tool calls are searched like any other, and what it did is
  in the work totals: `stats tools`, `stats files`, `convo tools` and the
  tool-call and file lines of `summary` and `diff` include subagent work,
  because that is why they are indexed. convo keeps no token counts, so there
  is nothing to leave out there.
- Counts of sessions leave subagents out, so a session that spawned forty
  helpers is still one session. That covers `convo sessions`, `convo projects`,
  `convo info`'s top projects, `convo inspect --latest`, and the session-based
  parts of `stats` (`sessions`, `commands`, `model`), `summary` and `diff`
  (session counts and durations, first messages, models). Pass
  `--include-subagents` to `sessions`, `projects`, `stats`, `summary` or `diff`
  to count them too. A subagent id is the one containing a dot.
- `--project` resolves among top-level sessions, since a subagent can run in a
  worktree of its own and its path would make a substring like `repo`
  ambiguous. `--include-subagents` widens that too, so a worktree path that only
  subagents used can be named.
- `convo info`'s `row_counts.sessions` is the table's row count, so it includes
  subagent sessions.

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
process umask, since convo data may include prompt/response content. convo
creates the live DB `0600` too and tightens its `-wal` / `-shm` sidecars to
`0600` on every open. On Windows the mode bits are ignored; see
[SECURITY.md](SECURITY.md). A nightly snapshot from cron:

```cron
0 3 * * * /path/to/convo backup --auto
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
