# Contributing to convo

Thanks for your interest. convo is a small, focused tool, so contributions
that align with its scope are welcome and ones that expand it are a
conversation.

## Development setup

Requirements: Python 3.12 or newer, [uv](https://docs.astral.sh/uv/),
and [just](https://github.com/casey/just) (optional but recommended).

```bash
git clone https://github.com/TracineHQ/convo
cd convo
uv sync --extra dev
uv run pre-commit install
```

## Pre-flight checks

Before opening a pull request, run:

```bash
just check
```

That runs ruff, mypy strict, format check, and the test suite with coverage.
If you do not have `just`, the equivalent commands are in the `justfile`.

## Code style

- Python 3.12+ syntax. Prefer `X | Y` over `Optional[X]`, modern type
  parameter syntax (PEP 695) where it helps, and structural pattern matching
  (`match`/`case`) when it reads better than nested ifs.
- Type annotations on every public function in `src/`. Tests are looser.
- Docstrings on public functions. Google-style.
- Coverage threshold: 80% minimum, climbing.

## Commit style

- One concern per commit.
- Imperative subject under 60 characters.
- No type prefixes (no `feat:`, `fix:`, etc.).
- Body only when context is non-obvious.
- Every commit must leave the repo in a green state (`just check`).

## Pull requests

- Branch off `main`. Open the PR against `main`.
- Describe what changed and why. Link issues by number.
- CI must be green. A reviewer must approve.
- Squash on merge if the branch has more than ~3 commits.

## Reporting issues

- Use the bug template for bugs and the feature template for proposals.
- Include `convo --version`, your Python version, and the command you ran.
- Reproducers help. Synthetic JSONL fixtures are fine; do not paste real
  session content unless you have scrubbed it.

## Security

For security issues, do not open a public issue. See `SECURITY.md`.
