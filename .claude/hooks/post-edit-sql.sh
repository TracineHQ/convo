#!/usr/bin/env bash
# PostToolUse hook for Edit/Write/MultiEdit on .sql files under
# src/convo/migrations/. Pipes the file through `sqlite3 :memory:` to
# verify it parses; exits 2 with stderr on parse error.

set -euo pipefail

input=$(cat)
file_path=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')
[[ -z "$file_path" ]] && exit 0

project_dir="${CLAUDE_PROJECT_DIR:-$(pwd)}"
case "$file_path" in
    "$project_dir"/src/convo/migrations/*.sql) ;;
    *) exit 0 ;;
esac

[[ -f "$file_path" ]] || exit 0

if ! err=$(sqlite3 :memory: < "$file_path" 2>&1); then
    printf 'SQL parse error in %s:\n%s\n' "$file_path" "$err" >&2
    exit 2
fi
exit 0
