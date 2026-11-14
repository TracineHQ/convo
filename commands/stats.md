---
description: Tool-call frequency and error rates from indexed history.
argument-hint: "[--since SPAN] [--project <prefix>] [--include-sdk]"
allowed-tools: "Bash(convo stats *)"
disable-model-invocation: true
---

# /convo:stats

Wraps `convo stats`, which emits a v2 envelope when `--json` is passed.
Parses at `stats.*`. See `JSON-ENVELOPE.md` at the repo root for the
contract.

Print the `tools` stats family: call frequency and error rate per tool across
indexed interactive sessions, subagent work included. SDK sessions (headless
`claude -p`, Agent SDK runs) and their subagents are left out unless you pass
`--include-sdk`.

```!
convo stats tools ${ARGUMENTS}
```

Display the output above to the user verbatim.
