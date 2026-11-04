# Releasing agent-proxy

The distribution name is **`thinkeryzu-agent-proxy`**. The Python import is `agentproxy`
and the installed command is **`agent-proxy`**. uv installs packages from PyPI;
there is no separate uv registry. Older GitHub tags used distribution metadata
named `agent-proxy`; dependencies for upcoming releases must use `thinkeryzu-agent-proxy`.

v0.3.1 is the first PyPI release. The earlier v0.3.0 GitHub release remains
withdrawn; do not recreate that tag. Pushing a new authorized version tag starts
publication to PyPI and then GitHub.

## Workflow

| Job | Runs after | Responsibility | Permissions |
|---|---|---|---|
| `build` | a pushed `v*` tag | Check tag/version/notes, test, build with uv, validate metadata, test wheel/sdist/uv tool, upload the two distributions | repository read |
| `pypi` | successful build | Download the validated files and publish to PyPI with uv Trusted Publishing | OIDC token, `pypi` environment |
| `github` | successful PyPI upload | Attach those same files and release notes to a GitHub Release | repository write |

The PyPI job uses `uv publish --trusted-publishing always`; no stored API token
is needed. `--check-url https://pypi.org/simple/` permits retrying an upload
when the existing files match. Conflicting files fail instead of being replaced.
The GitHub Release is created only after PyPI succeeds. The tag itself exists
before publication, so inspect the workflow result before announcing a release.
uv and the new artifact/setup actions are pinned in release.yml.

## One-time Trusted Publisher setup

The publisher identity below is used by this repository. To set up or repair
it, configure it through the owning PyPI account:

1. Sign in to the maintainer's PyPI account and open
   [pending publishers](https://pypi.org/manage/account/publishing/).
   Use a pending publisher for a new project, or the existing project's
   Publishing settings once the first release has created it:

   | Field | Value |
   |---|---|
   | PyPI project | `thinkeryzu-agent-proxy` |
   | GitHub owner | `ThinkerYzu` |
   | Repository | `agent-proxy` |
   | Workflow filename | `release.yml` |
   | Environment | `pypi` |

   PyPI also asks for an email address when creating a pending publisher.
   Complete that through your account, not a repository secret.
2. Create the `pypi` environment in
   [repository settings](https://github.com/ThinkerYzu/agent-proxy/settings/environments)
   if it does not exist. The name must match both the workflow and PyPI.
3. Confirm that the account owns the chosen name. A 404 response from PyPI
   indicates no current project; it does not reserve the name. The names
   `agent-proxy` and `ai-agent-proxy` belong to other projects; `agentproxy`
   was rejected for name similarity. None should be substituted for
   `thinkeryzu-agent-proxy`.

See the official [PyPI Trusted Publishing instructions](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
and [uv GitHub Actions publishing guide](https://docs.astral.sh/uv/guides/integration/github/#publishing-to-pypi).
Missing/mismatched publisher configuration fails the upload and prevents the
GitHub release job from running.

## Prepare a release

1. Run all 20 native tests with Claude and Codex installed and Codex signed in
   with ChatGPT. They spend quota and take several minutes:

   ```bash
   AGENTPROXY_RUN_LIVE=1 pytest -m live
   ```

   Check for 20 passes without skips and record installed backend versions.
   CI cannot perform these checks because it has no native agent binaries.
2. Choose a version, update `pyproject.toml` and
   `src/agentproxy/__init__.py`, and write `RELEASE-NOTES.md`.
   The first line must be exactly `# agent-proxy v<VERSION>` with no withdrawn
   or draft suffix. A withdrawn or draft heading deliberately fails this check.
   Do not reuse a withdrawn release without a new explicit decision.
3. Validate locally, building into an empty output directory:

   ```bash
   uv build --no-sources --out-dir /tmp/agentproxy-dist
   python tools/check_release.py v<VERSION> --dist /tmp/agentproxy-dist
   python -m twine check --strict /tmp/agentproxy-dist/*
   ```

   Install the wheel and source distribution in separate clean Python 3.11
   environments. Run offline tests, replay with `--assert-drained`, check both
   package/module versions and the CLI, and smoke-test the wheel using
   `uv tool install`. release.yml contains the exact verification commands.
4. Update README/API/release-note status and installation instructions when
   publishing. Preserve the published release information during preparation.
   Commit the package changes, workflow, helper and public documentation.

Preparation alone does not authorize a tag, PyPI upload or GitHub Release.

## Publish an authorized release

1. Verify the one-time publisher setup, a clean checkout, the intended commit,
   the matching tag/package/module versions and successful native validation.
2. Push master and check CI on Python 3.11–3.14 before pushing the version tag:

   ```bash
   git push origin master
   git tag v<VERSION>
   git push origin v<VERSION>
   ```

   Replace `<VERSION>` with the chosen version. The tag starts publication.
3. Check all three jobs. If PyPI succeeded but GitHub publication failed,
   retry the failed job using the original validated artifact. Do not rebuild
   different files under an already-published version.
4. Verify the PyPI project/version and its hashes, install from PyPI using
   pip and uv in clean environments, and compare the GitHub assets with the
   files on PyPI. Verify the console entry point and drained replay.

CI runs offline tests and drained replay on Python 3.11–3.14 on pushes/PRs.
Release validation adds strict metadata checks and distribution installation
checks. Neither replaces the live tests.

## Installation

Published versions can be installed from PyPI using pip or uv:

```bash
uv tool install thinkeryzu-agent-proxy
agent-proxy --help
uvx --from thinkeryzu-agent-proxy agent-proxy --help
uv pip install thinkeryzu-agent-proxy
pip install thinkeryzu-agent-proxy
```

Use `thinkeryzu-agent-proxy==<VERSION>` instead of `thinkeryzu-agent-proxy` to pin a release.
Claude/Codex are separately installed binaries; pip/uv cannot install or sign
in to them. GitHub tags remain an alternative:

```bash
pip install 'git+https://github.com/ThinkerYzu/agent-proxy@v0.3.1'
```

## Cancelling or withdrawing

Before publication, cancel the active workflow and verify it stopped. Once
PyPI accepts a version, deleting the GitHub release/tag does not undo that
upload. Use PyPI's [yank feature](https://docs.pypi.org/project-management/yanking/)
through the owning account and document the reason. A yank does not recall
installed or downloaded copies; exact version pins can still select it.
GitHub release/tag removal is a separate action. Never automatically roll back
one destination by deleting packages from another.

## Versioning

Additive commands/events/fields require a minor bump. Compatible fixes require
a patch bump. Pre-1.0, document any breaking wire-contract change prominently.
Clients must ignore unknown fields/types and inspect all outstanding counters.
