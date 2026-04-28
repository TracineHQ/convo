# convo

Global conversation index and query tool for Claude Code. Indexes JSONL session
files into a single SQLite database with full-text search, then makes it queryable
for tool-call analytics, anti-pattern detection, and session inspection across
every project on your machine.

Status: under construction. v0.1.0 in progress.

## Planned commands

- `convo summary` -- one-shot dashboard across sessions, tools, dangers, anti-patterns
- `convo search` -- substring or FTS search over tool calls, with filters and context
- `convo stats {tools, commands, sessions, files, skills, model, hooks}` -- analytics
- `convo diff` -- compare current period vs previous (default 7d)
- `convo inspect` -- session timeline and subagent tree view
- `convo index` -- build / update the index incrementally
- `convo backup` -- snapshot the database

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
