---
description: Compare the current activity window against the previous window of the same length.
argument-hint: "[--since SPAN] [--include-subagents]"
allowed-tools: "Bash(convo diff *)"
disable-model-invocation: true
---

# /convo:diff

Wraps `convo diff`, which emits a v2 envelope when `--json` is passed.
Parses at `diff.deltas.*`. See `JSON-ENVELOPE.md` at the repo root for
the contract.

Print convo's window-over-window diff. Defaults to 7d (current 7d vs previous 7d).
Session and file counts leave subagents out unless you pass `--include-subagents`;
tool-call totals always include subagent work.

```!
convo diff ${ARGUMENTS:---since 7d}
```

Display the output above to the user verbatim.
