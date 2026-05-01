# Releasing convo

Maintainer-facing checklist for cutting a release. Follow top to bottom; do not
skip steps. `X.Y.Z` below means the version you are releasing (no leading `v`).
Tags are `vX.Y.Z`.

This repo's convention: `pyproject.toml` `version` always reflects the
**last released version** on `main`. Bump only when the next release is
actually being prepared — no `-dev0` suffix on `main` between releases.

## 1. Pre-tag checklist

- [ ] `main` is green: latest `CI`, `CodeQL`, and `Scorecard` workflow runs all
      passing on the commit you intend to tag.
- [ ] Working tree clean: `git status` shows nothing to commit.
- [ ] `CHANGELOG.md` has a real `## [X.Y.Z] - YYYY-MM-DD` heading directly above
      `## [Unreleased]`. The entry covers user-facing notes under the relevant
      Keep-a-Changelog buckets: Added / Changed / Fixed / Removed / Deprecated /
      Security. No empty buckets.
- [ ] `pyproject.toml` `version = "X.Y.Z"` matches the planned tag (no `v`,
      no `-dev` suffix).
- [ ] `.claude-plugin/plugin.json` `version` matches `X.Y.Z`.
- [ ] `.claude-plugin/marketplace.json` plugin entry `version` matches `X.Y.Z`.
- [ ] Version-consistency test passes:
      `uv run pytest tests/test_version_consistency.py`.
- [ ] Full local check is clean: `just check` (ruff + mypy + pytest + coverage).
- [ ] Local wheel build reports the right CLI version:

      ```bash
      uv build
      uv tool install ./dist/convo-X.Y.Z-*.whl --reinstall
      convo --version    # expect: convo X.Y.Z
      uv tool uninstall convo
      ```

## 2. Tag and push

- [ ] Create the tag (signed if your git config has GPG/SSH signing set up):

      ```bash
      git tag -s vX.Y.Z -m "vX.Y.Z"
      # If signing is not configured:
      # git tag vX.Y.Z -m "vX.Y.Z"
      ```

- [ ] Push the tag:

      ```bash
      git push origin vX.Y.Z
      ```

- [ ] The release workflow (`.github/workflows/release.yml`) fires automatically
      on tag push matching `v*`. It builds the sdist + wheel, runs Trusted
      Publisher (OIDC) upload to PyPI, and drafts a GitHub Release with the
      artifacts attached and CHANGELOG notes for `X.Y.Z` populated as the body.

## 3. Post-tag verification

- [ ] Open the run under the **Release** workflow in GitHub Actions.
- [ ] The `publish-to-pypi` job pauses on the protected `pypi` environment
      (required reviewers). Approve the deployment.
- [ ] Verify the release on [pypi.org/project/convo](https://pypi.org/project/convo/).
      The new version must be the latest. Check that both wheel and sdist are
      present.
- [ ] Verify the GitHub Release exists at
      `https://github.com/TracineHQ/convo/releases/tag/vX.Y.Z` with:
  - [ ] `convo-X.Y.Z-py3-none-any.whl` attached.
  - [ ] `convo-X.Y.Z.tar.gz` attached.
  - [ ] Body populated from the `## [X.Y.Z]` CHANGELOG section.
  - [ ] If it landed as draft, click **Publish release**.
- [ ] Smoke-install from PyPI in a clean shell:

      ```bash
      pipx install convo==X.Y.Z
      convo --version    # expect: convo X.Y.Z
      convo info         # sanity: opens a DB without crashing
      pipx uninstall convo
      ```

## 4. Claude Code marketplace submission

- [ ] Confirm `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`
      reflect the released `X.Y.Z` (already validated in section 1).
- [ ] Submit to Anthropic's plugin directory. As of 2026-05 the documented
      submission entrypoint is the form at
      [clau.de/plugin-directory-submission](https://clau.de/plugin-directory-submission)
      (also reachable as
      [platform.claude.com/plugins/submit](https://platform.claude.com/plugins/submit)).
      The community marketplace mirror lives at
      [anthropics/claude-plugins-community](https://github.com/anthropics/claude-plugins-community)
      (read-only — do not open a PR there). **Verify the current flow before
      submitting** — Anthropic has iterated on this and the form URL is the
      authoritative source.
- [ ] Submission requires the public repo URL and the path to
      `.claude-plugin/marketplace.json`. Plugins go through automated review
      before listing; "Anthropic Verified" is a separate, additional review.
- [ ] Wait for the acceptance email / GitHub notification.
- [ ] Once listed, smoke-test from inside Claude Code:

      ```
      /plugin marketplace add TracineHQ/convo
      /plugin install convo
      ```

      Confirm the plugin appears and the version matches `X.Y.Z`.

## 5. Post-release housekeeping

- [ ] Add a fresh `## [Unreleased]` heading to the top of `CHANGELOG.md` with
      empty Added / Changed / Fixed buckets ready to receive entries.
- [ ] **Do not** bump `pyproject.toml` `version` here. This repo keeps `main`
      at the last released version; the bump happens at the start of the next
      release in section 1.
- [ ] If a breaking change is anticipated for the next release, open a
      `vX.(Y+1).0` milestone now and pin the relevant issues to it.
- [ ] Commit:

      ```
      Open X.Y.(Z+1) development
      ```

      (or `Open X.(Y+1).0 development` for the next minor.)

## 6. Backout / hotfix procedure

If `X.Y.Z` ships with a critical bug after PyPI publish:

- [ ] **Yank, do not delete.** Yanking hides the version from resolvers but
      preserves the artifact for anyone who pinned it explicitly. Deletion is
      irreversible and breaks reproducibility. See
      [PyPI yanking docs](https://docs.pypi.org/project-management/yanking/)
      and [PEP 592](https://peps.python.org/pep-0592/).
- [ ] Yank via the PyPI web UI:
      `https://pypi.org/manage/project/convo/release/X.Y.Z/` →
      **Options** → **Yank**. `uv publish` does not expose a yank command.
- [ ] **Do not delete the git tag.** Leave `vX.Y.Z` in history so the bug is
      traceable.
- [ ] Cut a hotfix:
  1. Branch from `main` (or from `vX.Y.Z` if `main` has already moved).
  2. Fix the bug, add a test, update `CHANGELOG.md` under a new
     `## [X.Y.(Z+1)] - YYYY-MM-DD` entry describing what broke and what was
     fixed under **Fixed** (and **Security** if applicable).
  3. Bump `pyproject.toml`, `plugin.json`, and `marketplace.json` to
     `X.Y.(Z+1)`.
  4. Run section 1 in full, then section 2 with the new tag.
- [ ] After the fix is live on PyPI, optionally update the yanked release's
      yank reason via the PyPI UI to point at the replacement version.

## Quick reference: files that carry the version

| File                                | Field                          |
|-------------------------------------|--------------------------------|
| `pyproject.toml`                    | `[project] version`            |
| `.claude-plugin/plugin.json`        | `version`                      |
| `.claude-plugin/marketplace.json`   | `plugins[0].version`           |
| `CHANGELOG.md`                      | `## [X.Y.Z] - YYYY-MM-DD`      |
| `src/convo/__init__.py`             | `__version__` (read from pkg)  |

The version-consistency test (`tests/test_version_consistency.py`) is the
guardrail: keep these in sync or the test fails before tagging.
