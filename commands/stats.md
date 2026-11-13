---
description: Tool-call frequency and error rates from indexed history.
allowed-tools: "Bash(convo stats *)"
disable-model-invocation: true
---

# /convo:stats

Wraps `convo stats`, which emits a v2 envelope when `--json` is passed.
Parses at `stats.*`. See `JSON-ENVELOPE.md` at the repo root for the
contract.

Print the `tools` stats family: call frequency and error rate per tool across
indexed interactive sessions. SDK sessions (headless `claude -p`, Agent SDK
runs) are left out; `convo stats tools --include-sdk` counts them.

```!
convo stats tools
```

Display the output above to the user verbatim.
