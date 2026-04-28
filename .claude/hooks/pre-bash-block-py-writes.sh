#!/usr/bin/env bash
# PreToolUse hook for Bash. Blocks bash commands that would write to a
# Python source file under src/convo/ or tests/ (via redirect, tee, or
# sed -i). Forces the agent to use Edit / Write / MultiEdit instead so the
# post-edit-python.sh PostToolUse hook always runs.
#
# Stdin: tool input JSON from Claude Code.
# Required tools: jq.
#
# Exit 2 + stderr is fed back to Claude as actionable feedback.

set -uo pipefail

input=$(cat)
command=$(printf '%s' "$input" | jq -r '.tool_input.command // empty')

if [[ -z "$command" ]]; then
    exit 0
fi

# Patterns to block (case-insensitive on the keyword, paths kept literal):
#   1. Output redirect into a .py file under src/convo or tests/
#        > src/convo/foo.py    >> tests/test_x.py
#   2. tee writing to one of those files
#        | tee src/convo/foo.py     | tee -a tests/test_x.py
#   3. sed -i editing one of those files in place
#        sed -i 's/x/y/' src/convo/foo.py
#
# We deliberately allow writes anywhere outside src/convo/ and tests/ (e.g.
# /tmp scratch files, build artifacts, generated docs).

# Bash =~ uses POSIX ERE (no non-greedy quantifiers, no lookarounds), so we
# match the path token greedily and rely on the alternation anchor inside it.
py_path='(src/convo/|tests/)[^[:space:]<>|;&]+\.py'

block=""

if [[ "$command" =~ \>\>?[[:space:]]*[^[:space:]]*${py_path} ]]; then
    block="output redirect"
elif [[ "$command" =~ (^|[[:space:];|\&\|])tee[[:space:]]+(-a[[:space:]]+)?[^[:space:]]*${py_path} ]]; then
    block="tee"
elif [[ "$command" =~ (^|[[:space:];|\&])sed[[:space:]]+-i[^[:space:]]*([[:space:]]+[^[:space:]]+)*[[:space:]]+[^[:space:]]*${py_path} ]]; then
    block="sed -i"
fi

if [[ -n "$block" ]]; then
    cat >&2 <<EOF
Blocked: this bash command would modify a Python source file in this repo via "$block".

Use the Edit, Write, or MultiEdit tool instead. Those trigger the post-edit hook (ruff fix + format + mypy) automatically; bash redirects bypass it.

Command:
$command
EOF
    exit 2
fi

exit 0
