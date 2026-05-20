---
description: List all tool names seen in indexed sessions, sorted by call frequency.
argument-hint: "[--limit N]"
allowed-tools: "Bash(convo tools *)"
disable-model-invocation: true
---

# /convo:tools

Wraps `convo tools`, which emits a v2 envelope when `--format=json` is passed.
Parses at `tools.items[*]`. See `JSON-ENVELOPE.md` at the repo root for
the contract.

List every tool name that appears in the convo DB, ordered by call count
descending. Use this to discover which tool names to pass to `--tool` filters
in search commands.

```!
convo tools ${ARGUMENTS}
```

Display the output above to the user verbatim.
