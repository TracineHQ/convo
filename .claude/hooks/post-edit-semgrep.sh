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

set -uo pipefail

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
out=$(
    uvx --from 'semgrep==1.159.0' semgrep \
        --config=.semgrep/ \
        --error \
        --quiet \
        --metrics=off \
        --no-git-ignore \
        "$file_path" 2>&1 || true
)

# Strip the "no findings" banner; only surface real findings.
if printf '%s' "$out" | grep -qE '(❯❱|Severity:|Finding:|Findings:)'; then
    printf '%s\n' "$out" >&2
    exit 2
fi

exit 0
