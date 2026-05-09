# Security policy

## Supported versions

The latest minor release receives security fixes. The v0.1.x line is the
current supported series.

| Version | Supported |
|---|---|
| 0.1.x | yes |
| < 0.1 | no |

## Reporting a vulnerability

**Do not open a public issue for security reports.**

Use GitHub Private Vulnerability Reporting:

> <https://github.com/TracineHQ/convo/security/advisories/new>

Please include:

- A description of the issue and its impact.
- Reproduction steps (commands, inputs, environment).
- Affected version (`convo --version`).
- Contact info for follow-up.

## Response targets

- **Acknowledgement:** within 72 hours of report.
- **High-severity fix:** within 30 days of triage.
- **Coordinated disclosure:** roughly a 7-day public-update window after the
  fix lands, to give users time to upgrade before details become public.

## Threat model

convo reads Claude Code session JSONLs from `~/.claude/projects/` and persists
their content to a local SQLite database. Inputs are user-controlled but the
JSONLs themselves can carry attacker-influenceable text — pasted prompts,
fetched web content, MCP tool responses. The intake pipeline is written with
that in mind:

- JSONL parsing is stdlib `json.loads`; no `eval`/`exec`/`pickle`.
- Per-file errors are contained: a malformed line aborts that file's
  transaction only, never the tree run.
- All SQL parameters are bound via `?`-placeholders. Constants interpolated
  via f-strings are typed literals (e.g. integer table limits, float
  `SECONDS_PER_DAY`).
- The live DB and snapshot files are written `0o600` on POSIX; WAL/SHM
  sidecars are chmodded to match. **On Windows, POSIX mode bits are ignored
  by the OS** — `os.open(..., 0o600)` and `Path.chmod(0o600)` succeed but
  do not restrict access. The DB inherits the parent directory's ACL,
  which is owner-only for the typical `%USERPROFILE%\.claude\` install but
  not guaranteed for shared-user or CI environments. Tightening to
  per-user ACL on Windows is a known limitation.
- Restore is atomic-replace and same-filesystem only; staging files are
  unlinked on partial failure.

## What's in scope

- The `convo` Python package and CLI.
- The CI configuration and pre-commit hooks shipped in this repo.
- The Claude Code plugin manifest under `.claude-plugin/`.

## What's out of scope

- Issues in upstream dependencies (file those upstream and link the
  advisory here once available).
- The contents of users' local Claude Code session files.
- Arbitrary input to the FTS5 query parser is treated as a search query;
  malformed FTS5 syntax is a UX issue, not a security one.
