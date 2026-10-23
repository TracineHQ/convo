---
description: Activity dashboard for the last 7 days (tools, commands, sessions, files, model).
argument-hint: "[--since SPAN] [--include-subagents]"
allowed-tools: "Bash(convo summary *)"
disable-model-invocation: true
---

# /convo:summary

Wraps `convo summary`, which emits a v2 envelope when `--json` is
passed. Parses at `summary.*`. See `JSON-ENVELOPE.md` at the repo root
for the contract.

Print convo's activity rollup. Defaults to the last 7 days; pass
`--since 30d` (or any `<N><unit>` span) to widen the window. Session counts
leave subagent sessions out unless you pass `--include-subagents`; tool-call
and file totals always include subagent work.

```!
convo summary ${ARGUMENTS:---since 7d}
```

Display the output above to the user verbatim.
