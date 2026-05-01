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

# Run security scanners: bandit (static AST) + pip-audit (CVE check on deps)
security:
    uv run --with bandit bandit -c pyproject.toml -r src/ -ll
    uv export --format requirements-txt --no-hashes --no-emit-project --extra dev > requirements-audit.txt
    uv run --with pip-audit pip-audit -r requirements-audit.txt --strict --disable-pip --no-deps
    rm -f requirements-audit.txt
