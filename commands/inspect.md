---
description: Show a session's full message timeline. Accepts a session-id prefix (8+ chars) or --latest.
argument-hint: <session-id-prefix | --latest>
allowed-tools: Bash(convo inspect *)
disable-model-invocation: true
---

# /convo:inspect

Wraps `convo inspect`, which emits a v2 envelope when `--format=json` is
passed. Parses at `data.inspect.messages[*]`. See `JSON-ENVELOPE.md` at the
repo root for the contract.

Print the full message timeline for a session. Pass an 8+ character session-id
prefix, or `--latest` to inspect the most recently started session.

```!
convo inspect $ARGUMENTS
```

Display the output above to the user verbatim. If the prefix is ambiguous,
convo will list candidates — surface those candidates directly.
