---
description: Tool-call frequency and error rates from indexed history.
allowed-tools: Bash(convo stats *)
disable-model-invocation: true
---

# /convo:stats

Wraps `convo stats`, which emits a v2 envelope when `--format=json` is passed.
Parses at `stats.*`. See `JSON-ENVELOPE.md` at the repo root for the
contract.

Print the `tools` stats family: call frequency, median duration, and error
rate per tool across all indexed sessions.

```!
convo stats tools
```

Display the output above to the user verbatim.
