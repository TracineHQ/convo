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

set -euo pipefail

input=$(cat)
command=$(printf '%s' "$input" | jq -r '.tool_input.command // empty')

if [[ -z "$command" ]]; then
    exit 0
fi

# Strip shell line comments so a path token inside a `#`-comment can't
# trigger a false positive. Per-line so multi-line / `;`-joined commands
# still get each line scrubbed. Imperfect for `#` inside quoted strings, but
# the prior version had no comment handling at all and live triggered on a
# comment containing `> /Users/.../src/convo/foo.py`.
command_clean=$(printf '%s\n' "$command" | sed -E 's/(^|[[:space:]])#.*$//')

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

# Match relative AND absolute paths under src/convo/ or tests/. Prior version
# only matched the relative shape, so `> /Users/dev/.../src/convo/foo.py`
# bypassed the block. Bash =~ uses POSIX ERE (no non-greedy / lookaround),
# so we match the path prefix greedily and anchor on the literal segment
# names.
py_path='([^[:space:]<>|;&]*/)?(src/convo/|tests/)[^[:space:]<>|;&]+\.py'

block=""

if [[ "$command_clean" =~ \>\>?[[:space:]]*[^[:space:]]*${py_path} ]]; then
    block="output redirect"
elif [[ "$command_clean" =~ (^|[[:space:];|\&\|])tee[[:space:]]+(-a[[:space:]]+)?[^[:space:]]*${py_path} ]]; then
    block="tee"
elif [[ "$command_clean" =~ (^|[[:space:];|\&])sed[[:space:]]+-i[^[:space:]]*([[:space:]]+[^[:space:]]+)*[[:space:]]+[^[:space:]]*${py_path} ]]; then
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
