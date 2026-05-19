---
description: Activity dashboard for the last 7 days (tools, commands, sessions, files, model).
argument-hint: [--since SPAN]
allowed-tools: Bash(convo summary *)
disable-model-invocation: true
---

# /convo:summary

Wraps `convo summary`, which emits a v2 envelope when `--format=json` is
passed. Parses at `data.summary.*`. See `JSON-ENVELOPE.md` at the repo root
for the contract.

Print convo's activity rollup. Defaults to the last 7 days; pass
`--since 30d` (or any `<N><unit>` span) to widen the window.

```!
convo summary ${ARGUMENTS:---since 7d}
```

Display the output above to the user verbatim.
