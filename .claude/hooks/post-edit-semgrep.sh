#!/usr/bin/env bash
# PostToolUse hook for Edit / Write / MultiEdit on Python files.
#
# Runs the project's local semgrep rules (.semgrep/) against the touched file.
# - Exits 0 with no output when clean.
# - Exits 2 with stderr when a finding fires; Claude Code surfaces stderr as
#   feedback so the agent can fix it on the next turn.
#
# Stdin: tool input JSON from Claude Code.
# Required tools: jq, uv. Semgrep is fetched on demand via `uvx`.

set -euo pipefail

input=$(cat)
file_path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')

if [[ -z "$file_path" ]]; then
    exit 0
fi

project_dir="${CLAUDE_PROJECT_DIR:-$(pwd)}"
if [[ "$file_path" != "$project_dir"/* ]] || [[ "$file_path" != *.py ]]; then
    exit 0
fi

case "$file_path" in
    "$project_dir"/.venv/* | "$project_dir"/dist/* | "$project_dir"/build/* | "$project_dir"/.git/*)
        exit 0
        ;;
esac

if [[ ! -f "$file_path" ]]; then
    exit 0
fi

cd "$project_dir" || exit 0

# Pinned to the same version the pre-commit hook uses (.pre-commit-config.yaml).
# `uvx` caches across runs, so the second invocation is fast.
#
# Branch on semgrep's exit code, not on output text. With `--error`, semgrep
# returns non-zero whenever a rule fires; the prior emoji/keyword grep on
# stdout was fragile (would silently pass if semgrep changed output format
# or locale stripped the emoji glyphs) and also swallowed crashes.
if out=$(
    uvx --from 'semgrep==1.159.0' semgrep \
        --config=.semgrep/ \
        --error \
        --quiet \
        --metrics=off \
        --no-git-ignore \
        "$file_path" 2>&1
); then
    exit 0
fi

printf '%s\n' "$out" >&2
exit 2
