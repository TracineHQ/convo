---
description: Compare the current activity window against the previous window of the same length.
argument-hint: [--since SPAN]
allowed-tools: Bash(convo diff *)
disable-model-invocation: true
---

# /convo:diff

Wraps `convo diff`, which emits a v2 envelope when `--format=json` is passed.
Parses at `diff.deltas.*`. See `JSON-ENVELOPE.md` at the repo root for
the contract.

Print convo's window-over-window diff. Defaults to 7d (current 7d vs previous 7d).

```!
convo diff ${ARGUMENTS:---since 7d}
```

Display the output above to the user verbatim.
