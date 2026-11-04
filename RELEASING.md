# Releasing agent-proxy

Two GitHub Actions workflows in `.github/workflows/` carry the process:

| Workflow | Fires on | What it does |
|---|---|---|
| `ci.yml` | every push to `master`, every pull request | runs the suite on Python 3.11–3.14, then replays the recorded session and asserts the counters drain |
| `release.yml` | a pushed `v*` tag | checks the tag against the package version, runs the tests, builds the sdist and wheel, proves the wheel works, and publishes a GitHub Release with both artifacts |

Neither runner has `claude` or `codex` on it. That is not a gap in coverage but
the design: every test outside `tests/live/` reasons about recorded shapes — a
transcript for `claude`, a stand-in app-server for `codex` — so CI proves the
accounting and says nothing about whether the marker set still matches the
`claude` you have installed, or whether the app-server still sends the messages
the fake does. Only a live run answers that — see
[Before you tag](#before-you-tag).

## Cutting a release

1. **Run all 20 live tests.** They spend real quota and take several minutes:
   13 Claude cases and 7 Codex cases, including questions, permission
   approval/denial/expiry, cancellation and session recovery. These are the
   only checks that read what each installed binary actually writes:

   ```bash
   AGENTPROXY_RUN_LIVE=1 pytest -m live
   ```

   Install both binaries and sign Codex in with ChatGPT. Check that all
   20 cases passed without skips. Record agent versions: native hooks,
   transcript shapes and the experimental app-server protocol can change.

2. **Bump the version** in the two places the release job compares — they must
   match the tag or the job stops before building:

   - `pyproject.toml` → `[project] version`
   - `src/agentproxy/__init__.py` → `__version__`

3. **Update `RELEASE-NOTES.md`** with the changes since the previous tag,
   client compatibility, and required agent versions. The release job uses
   this file as the GitHub Release description. Update the README status and
   installation example when publishing the release.

4. **Commit** the bump and release notes:

   ```bash
   git add pyproject.toml src/agentproxy/__init__.py RELEASE-NOTES.md README.md API.md RELEASING.md .github/workflows/release.yml
   git commit -m "Prepare v0.3.0 release"
   ```

5. **Tag and push** — the tag is `v` + the version:

   ```bash
   git tag v0.3.0
   git push origin master
   git push origin v0.3.0
   ```

6. The Release workflow publishes to
   `https://github.com/ThinkerYzu/agent-proxy/releases`. If nothing appears,
   the Actions tab has the reason.

## Preparing without publishing

A request to prepare a release covers version changes, release notes and local
validation. Keep the README's published release and installation example at
the existing tag until publication. Build with `uv build` or `python -m build`,
verify the wheel in a clean Python 3.11 environment, run the offline suite and
replay with `--assert-drained`, and record artifact hashes. Do not create or
push a tag or publish a GitHub Release during preparation.

Before publishing a prepared release, update README status and installation examples,
remove the preparation markers from API.md and RELEASE-NOTES.md, commit the
prepared changes, then follow the tag/push steps above. Confirm hosted CI and
the release workflow, and verify downloaded artifacts.

## Before you tag

The version number is a promise to clients, and [API.md](API.md) is what the
promise is about. Before bumping, check the wire contract:

- **A message type, field, or command added** — clients that ignore unknown
  types keep working, so it is a minor bump.
- **A field removed or its meaning changed, or an ordering guarantee weakened**
  — that breaks a client silently, which is the failure mode this project
  exists to end. Bump the minor pre-1.0 and say so in the release description;
  update API.md in the same commit.
- **Behaviour changed with the contract intact** — a new end-of-turn marker, a
  new fallback condition — a patch bump, and worth a line in the notes because
  the `basis` values clients log will change.

## Versioning

Semantic-ish: patch for fixes, minor for new capability, major for breaking
changes. Pre-1.0, minor bumps may break things — call that out in the release
description.

## Installing a release

Not on PyPI. Clients install from the tag:

```bash
pip install 'git+https://github.com/ThinkerYzu/agent-proxy@v0.3.0'
```

which puts an `agent-proxy` binary on the path — the only thing
[r2d2box](https://github.com/ThinkerYzu/r2d2box) and
[agent-desktop-env](https://github.com/ThinkerYzu/agent-desktop-env) need from
this repo. The Release page also carries the sdist and wheel for anyone
installing without git.

Publishing to PyPI would need a decision this project has not made: the package
is useless without a `claude` or `codex` binary it cannot declare as a dependency. If it
ever happens, configure Trusted Publishing for the repo and add a job to
`release.yml` — memscout's `RELEASING.md` sketches one.
