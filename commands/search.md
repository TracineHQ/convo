---
description: Search Claude Code session history (FTS5 over messages, tool calls, tool results).
argument-hint: "<query>"
allowed-tools: "Bash(convo search *)"
disable-model-invocation: true
---

# /convo:search

Wraps `convo search`, which emits a v2 envelope when `--format=json` is
passed. Parses at `search.hits[*]`. See `JSON-ENVELOPE.md` at the repo
root for the contract.

Run convo's full-text search over indexed session history and display the
results verbatim.

```!
convo search "$ARGUMENTS" --limit 20
```

Display the output above to the user exactly as printed. Do not summarize or
paraphrase. If the output indicates the query is too short or the database is
empty, surface that message directly.
