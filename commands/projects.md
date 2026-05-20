---
description: List all indexed projects sorted by session count.
argument-hint: "[--limit N]"
allowed-tools: "Bash(convo projects *)"
disable-model-invocation: true
---

# /convo:projects

Wraps `convo projects`, which emits a v2 envelope when `--format=json` is
passed. Parses at `projects.items[*]`. See `JSON-ENVELOPE.md` at the
repo root for the contract.

List all projects in the convo DB, ordered by session count descending.
Useful for discovering which project prefixes to pass to `--project` filters
in search and sessions commands.

```!
convo projects ${ARGUMENTS}
```

Display the output above to the user verbatim.
