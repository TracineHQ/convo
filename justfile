# convo dev tooling
# Run `just` with no args to list recipes.

default:
    @just --list

# Full pre-flight: lint, typecheck, format check, tests with coverage
check: lint typecheck format-check test

# Fast pre-flight: lint and tests only, skip slow checks
check-fast: lint test-fast

# Lint with ruff
lint:
    uv run ruff check .

# Auto-fix lint issues
lint-fix:
    uv run ruff check --fix .

# Static type check with mypy strict
typecheck:
    uv run mypy .

# Verify formatting (no changes)
format-check:
    uv run ruff format --check .

# Apply formatting
format:
    uv run ruff format .

# Run full test suite with coverage gating
test:
    uv run pytest --cov --cov-report=term-missing

# Run tests without coverage (faster local iteration)
test-fast:
    uv run pytest -q

# Run benchmark scripts (writes results under bench/results/)
bench:
    @echo "bench: TODO; tracked in CHANGELOG [Future work]"

# Live demo for README recordings
demo:
    @echo "demo: TODO; tracked in CHANGELOG [Future work]"

# Reset the local convo database (uses default location)
db-reset:
    rm -f ~/.claude/convo.db ~/.claude/convo.db-journal ~/.claude/convo.db-wal ~/.claude/convo.db-shm

# Reset the local snapshot directory
snapshots-clean:
    rm -rf ~/.claude/convo-backups

# Sync dev dependencies
sync:
    uv sync --extra dev

# Install pre-commit hooks
hooks-install:
    uv run pre-commit install

# Run pre-commit hooks against the entire tree
hooks-run:
    uv run pre-commit run --all-files

# Mirror the CI matrix locally as closely as possible
ci: lint typecheck format-check test
