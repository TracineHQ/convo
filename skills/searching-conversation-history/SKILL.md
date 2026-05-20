---
description: Search the user's prior Claude Code session history when they ask "did I solve this before?", "what was that fix for X?", "have I worked on Y?", or want a summary of past activity. Uses the `convo` CLI (FTS5 over messages, tool calls, tool results).
allowed-tools: Bash(convo search *) Bash(convo summary *) Bash(convo inspect *) Bash(convo projects *) Bash(convo tools *) Bash(convo sessions *) Bash(convo info)
---

# Searching conversation history

Use this skill when the user asks a question about their **own past Claude Code
sessions** -- not about the current codebase, not about general knowledge.

## When to invoke

Trigger phrases that should activate this skill:

- "Did I deal with this before?"
- "What was that fix for the kafka thing?"
- "Have I worked on this error?"
- "Find that session where we set up X"
- "Summarize last week" / "what did I do this week"
- "When did I last touch <file>?"
- Any recall question framed in first-person past tense about prior sessions.

Do **not** invoke for:

- Questions about the current repo or codebase (use Read/Grep/Glob).
- General programming questions (answer directly).
- Live debugging of the current session.

## Default output: structured prose

`convo` commands emit structured prose by default -- no flags needed. Output
includes headings, bullet points, and human-readable timestamps. Display it
verbatim unless the user asks for a different format.

## Discovery: what's in the DB

Before searching, orient yourself with the discovery commands:

```bash
convo projects          # all projects by session count
convo tools             # all tools by call frequency
convo sessions --since 7d --limit 10   # recent sessions
```

Use these to pick the right `--project` filter or to confirm the DB has data.

## Recall a topic / phrase

```bash
convo search "auth token expiry" --since 90d --limit 5
```

Extract 2-4 concrete tokens from the user's question. FTS5 needs at least
3 characters per token. Use `-token` to exclude a term. AND is the default
-- all supplied tokens must appear in the same record.

Widen to `--since 1y` if the first attempt returns nothing.

## Time-bounded summary

```bash
convo summary --since 7d
```

Use for "what did I do this week" / "summarize last month". Spans accept
`<N><unit>`: `7d`, `24h`, `2w`, `1y`.

## Drill into a session

Once you have a hit, pass the session ID (8+ char prefix) to inspect:

```bash
convo inspect <session-id-prefix>
```

For a focused timeline of tool calls only:

```bash
convo inspect <session-id-prefix> --timeline
```

## Narrow the output with --fields

When you only need a subset of columns, use `--fields` to project:

```bash
convo search "kafka migration" --fields session,kind,excerpt
```

## Project-scoped search

```bash
convo search "kafka migration" --project /Users/dev/develop/mf/uu-rolecapacity-bff --since 1y
```

The `--project` filter accepts an absolute path prefix.

## JSON output (when needed)

Add `--format=json` (or `--json`) to get machine-parseable output. The v2
envelope shape is `{"schema_version": 2, "<command>": {...}}`. See
`JSON-ENVELOPE.md` at the repo root for per-command field contracts.

## Response format

1. Run the most specific command first (search > summary > sessions).
2. Quote matching excerpts with session ID (first 8 chars) and timestamp.
3. Offer `convo inspect <prefix>` for the full transcript if the user wants more.
4. If `convo` is not on PATH or the DB is empty, tell the user to install
   convo (`pipx install tracine-convo`) and run `convo index`.

## Examples

### Example 1: topic recall

User: "Did I ever fix that thing where the auth token kept expiring?"

```bash
convo search "auth token expir" --since 90d --limit 5
```

Quote the matching excerpts and offer `convo inspect <prefix>` for the full
transcript.

### Example 2: weekly summary

User: "What have I been working on this week?"

```bash
convo summary --since 7d
```

Surface the top tools, top commands, and top files from the output.

### Example 3: project-scoped

User: "When did I last touch the kafka migration in the rc-bff repo?"

```bash
convo search "kafka migration" --project /Users/dev/develop/mf/uu-rolecapacity-bff --since 1y --limit 5
```
