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
      uv tool install ./dist/tracine_convo-X.Y.Z-*.whl --reinstall
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
- [ ] Verify the release on [pypi.org/project/tracine-convo](https://pypi.org/project/tracine-convo/).
      The new version must be the latest. Check that both wheel and sdist are
      present.
- [ ] Verify the GitHub Release exists at
      `https://github.com/TracineHQ/convo/releases/tag/vX.Y.Z` with:
  - [ ] `tracine_convo-X.Y.Z-py3-none-any.whl` attached.
  - [ ] `tracine_convo-X.Y.Z.tar.gz` attached.
  - [ ] Body populated from the `## [X.Y.Z]` CHANGELOG section.
  - [ ] If it landed as draft, click **Publish release**.
- [ ] Smoke-install from PyPI in a clean shell:

      ```bash
      pipx install tracine-convo==X.Y.Z
      convo --version    # expect: convo X.Y.Z
      convo info         # sanity: opens a DB without crashing
      pipx uninstall tracine-convo
      ```

## 4. Claude Code marketplace submission

The community marketplace mirror at
[anthropics/claude-plugins-community](https://github.com/anthropics/claude-plugins-community)
is read-only and synced nightly from Anthropic's internal review pipeline.
**PRs opened against that repo are auto-closed.** Submit via the in-app form
instead.

### Pre-submission checks

- [ ] `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`
      reflect the released `X.Y.Z` (already validated in section 1).
- [ ] Local plugin smoke (in any Claude Code session, against the source tree):
      ```
      claude --plugin-dir /path/to/convo
      ```
      Confirm `/convo:search`, `/convo:summary`, `/convo:diff`, `/convo:inspect`,
      `/convo:stats`, `/convo:info` autocomplete and run. Confirm the
      `searching-conversation-history` skill activates on a history-recall
      question. Confirm the SessionEnd hook fires (run `convo info` after
      ending a session and verify `last_indexed_at` advanced).
- [ ] PyPI publish is live at
      [pypi.org/project/tracine-convo](https://pypi.org/project/tracine-convo/) —
      the marketplace listing assumes users can `pipx install tracine-convo` to
      get the `convo` binary the plugin shells out to.

### Submit via the in-app form

Pick one entrypoint (both submit to the same review pipeline):

- **Claude.ai**: [claude.ai/settings/plugins/submit](https://claude.ai/settings/plugins/submit)
- **Console**: [platform.claude.com/plugins/submit](https://platform.claude.com/plugins/submit)

Submission inputs (have these ready):

- Public repo URL: `https://github.com/TracineHQ/convo`
- Path to plugin manifest: `.claude-plugin/plugin.json`
- Path to marketplace manifest: `.claude-plugin/marketplace.json`
- Released version: matches the `version` field in both manifests and the git
  tag `vX.Y.Z`
- Description: pulled from `marketplace.json` `plugins[0].description`
- Category: `developer-tools` (already set in `marketplace.json`)
- License: `Apache-2.0` (already in `plugin.json`)

### After submission

- [ ] Plugins go through automated security scanning before being added to
      the community marketplace. Wait for the acceptance email / GitHub
      notification.
- [ ] Once listed, smoke from a clean Claude Code session:

      ```
      /plugin marketplace add TracineHQ/convo
      /plugin install convo@convo-marketplace
      ```

      Confirm the plugin appears in `/plugin list` and the installed version
      matches `X.Y.Z`.

- [ ] "Anthropic Verified" status is a separate, additional review and is not
      automatic on first listing. Apply later via the same in-app form once
      the plugin has community traction.

### Updating after listing

The community-marketplace `marketplace.json` is synced nightly from
Anthropic's internal pipeline. To ship an update:

1. Cut a new release through this checklist (section 1 onward).
2. Re-run the submission form for the new version (or follow whatever
   "update existing listing" path the form provides — check before
   re-submitting from scratch).
3. The synced mirror picks up the new version on its next nightly sync.

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
      `https://pypi.org/manage/project/tracine-convo/release/X.Y.Z/` →
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
