---
description: Quick overview of the convo DB (row counts, last index time, top projects, snapshots).
allowed-tools: Bash(convo info)
disable-model-invocation: true
---

# /convo:info

Wraps `convo info`, which emits a v2 envelope when `--format=json` is passed.
Parses at `info.*`. See `JSON-ENVELOPE.md` at the repo root for the
contract.

Print convo's database overview: schema version, row counts, last index
timestamp, top projects by session count, and snapshot directory size.

```!
convo info
```

Display the output above to the user verbatim.
