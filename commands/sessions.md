---
description: List recent sessions, optionally filtered by project or time span.
argument-hint: [--project <prefix>] [--since <span>] [--limit N]
allowed-tools: Bash(convo sessions *)
disable-model-invocation: true
---

# /convo:sessions

Wraps `convo sessions`, which emits a v2 envelope when `--format=json` is
passed. Parses at `sessions.items[*]`. See `JSON-ENVELOPE.md` at the
repo root for the contract.

List sessions ordered by start time descending. Accepts `--project <prefix>`
to scope to a single project and `--since <span>` (e.g. `7d`, `2w`, `1y`)
to limit the time window. Use session IDs from this output with
`/convo:inspect` to drill into a specific session.

```!
convo sessions ${ARGUMENTS:---since 7d --limit 20}
```

Display the output above to the user verbatim.
