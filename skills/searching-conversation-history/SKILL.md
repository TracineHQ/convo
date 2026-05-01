---
description: Search the user's prior Claude Code session history when they ask "did I solve this before?", "what was that fix for X?", "have I worked on Y?", or want a summary of past activity. Uses the `convo` CLI (FTS5 over messages, tool calls, tool results).
allowed-tools: Bash(convo search *) Bash(convo summary *) Bash(convo inspect *) Bash(convo info)
---

# Searching conversation history

Use this skill when the user asks a question that's about their **own past
Claude Code sessions** — not about the current codebase, not about general
knowledge.

## When to invoke

Trigger phrases that should activate this skill:

- "Did I deal with this before?"
- "What was that fix for the kafka thing?"
- "Have I worked on this error?"
- "Find that session where we set up X"
- "Summarize last week" / "what did I do this week"
- "When did I last touch <file>?"
- Any recall question framed in first person past tense about prior sessions.

Do **not** invoke for:

- Questions about the current repo or codebase (use Read/Grep/Glob).
- General programming questions (answer directly).
- Live debugging of the current session.

## How to invoke

`convo` is a CLI on the user's PATH that indexes Claude Code session JSONLs
into a local SQLite DB. Always use `--json` so you can parse the response.

### Recall a topic / phrase

```bash
convo search "<extracted-keywords>" --since 30d --limit 5 --json
```

- Extract 2-4 concrete tokens from the user's question (e.g. "kafka migration",
  "auth token refresh"). FTS5 needs at least 3 characters per token.
- Use `+token` to require, `-token` to exclude.
- Default `--since 30d` for "recently"; widen to `1y` if nothing matches.
- `--limit 5` keeps the response small. Bump to 20 if the user wants more.

### Time-bounded summary

```bash
convo summary --since 7d --json
```

Use this for "what did I do this week" / "summarize last month". Spans accept
`<N><unit>`: `7d`, `24h`, `2w`, `1y`.

### Drill into a specific session

Once you have a hit, use the `session_id` from the JSON envelope:

```bash
convo inspect <session-id> --json
```

A session-id prefix (8+ chars) is enough.

## Response format

After calling convo:

1. Parse the JSON envelope (`{"schema_version": 1, "search": {"hits": [...]}}`).
2. Quote the matching excerpt and cite the `session_id` (first 8 chars) and
   `timestamp` for each hit.
3. If the user wants more detail, follow up with `convo inspect <prefix>`.
4. If `convo` is not on PATH or the DB is empty, tell the user to install
   convo (`pipx install tracine-convo`) and run `convo index`. Don't fail silently.

## Examples

### Example 1: topic recall

User: "Did I ever fix that thing where the auth token kept expiring?"

```bash
convo search "+auth +token expir" --since 90d --limit 5 --json
```

Then quote the matching excerpts and offer `convo inspect <prefix>` for the
full transcript.

### Example 2: weekly summary

User: "What have I been working on this week?"

```bash
convo summary --since 7d --json
```

Surface the top tools, top commands, top files from the JSON envelope.

### Example 3: project-scoped

User: "When did I last touch the kafka migration in the rc-bff repo?"

```bash
convo search "kafka migration" --project /Users/dev/develop/mf/uu-rolecapacity-bff --since 1y --limit 5 --json
```

The `project` filter takes the absolute path of the project directory.
