#!/usr/bin/env bash
# PostToolUse hook for Edit / Write / MultiEdit on Python files in this repo.
#
# Auto-fixes lint and formatting silently, then runs mypy strict on the file.
# - Exits 0 with no output when the file is clean (agent sees nothing).
# - Exits 2 with stderr when issues remain (Claude Code routes stderr back as
#   feedback so the agent can address them on the next turn).
#
# Stdin: tool input JSON from Claude Code.
# Required tools: jq, uv, the project's dev deps (`uv sync --extra dev`).

set -uo pipefail

# Read the tool input JSON from stdin.
input=$(cat)

# Extract the file path. Edit, Write, and MultiEdit all use `file_path`.
file_path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')

# Bail out for non-file edits (e.g. notebook tools, missing path).
if [[ -z "$file_path" ]]; then
    exit 0
fi

# Only act on this project's Python source and tests.
project_dir="${CLAUDE_PROJECT_DIR:-$(pwd)}"
case "$file_path" in
    "$project_dir"/src/convo/*.py | "$project_dir"/tests/*.py)
        ;;
    *)
        exit 0
        ;;
esac

# Verify the file still exists (Edit/Write should leave it on disk).
if [[ ! -f "$file_path" ]]; then
    exit 0
fi

cd "$project_dir" || exit 0

# Auto-fix lint and format silently. Both are safe idempotent operations.
# Redirect both stdout and stderr to /dev/null on the auto-fix pass; the
# final lint pass below reports anything that survived in concise form.
uv run ruff check --fix "$file_path" >/dev/null 2>&1 || true
uv run ruff format "$file_path" >/dev/null 2>&1 || true

# Run mypy strict on the single file. Capture output; mypy prints "Success:"
# on clean runs, which we filter so the agent only sees real errors.
mypy_out=$(
    uv run mypy --no-error-summary --no-pretty --no-color-output "$file_path" 2>&1 \
        | grep -v '^Success:' \
        | grep -v '^$' \
        || true
)

# If there is anything left after auto-fix, surface it as feedback (exit 2).
if [[ -n "$mypy_out" ]]; then
    printf '%s\n' "$mypy_out" >&2
    exit 2
fi

# Re-check lint after format in case formatting introduced a fixable warning
# that ruff's combined run did not catch on the first pass.
lint_out=$(
    uv run ruff check --quiet --output-format=concise "$file_path" 2>&1 || true
)
if [[ -n "$lint_out" ]]; then
    printf '%s\n' "$lint_out" >&2
    exit 2
fi

exit 0
