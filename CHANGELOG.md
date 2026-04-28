# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Initial repository scaffold: license, README skeleton, pyproject, ruff +
  mypy strict configuration, justfile, pre-commit hooks, gitleaks config,
  GitHub Actions CI matrix on Python 3.12 and 3.13.

### Planned for 0.1.0

- Storage layer (schema, db, backup).
- JSONL intake pipeline with typed records.
- Read commands: search, format, inspect, export.
- Analytics: tools, commands, sessions, files, skills, model, hooks,
  retries, chains.
- CLI dispatch via typer.
- Period-comparison `diff` command.

### Future work

- `migrate` command for legacy observability databases.
- Demo asciinema recording in README.
- Optional mkdocs site.
- PyPI publish (deferred until adoption signal).
