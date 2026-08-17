# Releasing agent-proxy

Two GitHub Actions workflows in `.github/workflows/` carry the process:

| Workflow | Fires on | What it does |
|---|---|---|
| `ci.yml` | every push to `master`, every pull request | runs the suite on Python 3.11–3.14, then replays the recorded session and asserts the counters drain |
| `release.yml` | a pushed `v*` tag | checks the tag against the package version, runs the tests, builds the sdist and wheel, proves the wheel works, and publishes a GitHub Release with both artifacts |

Neither runner has `claude` on it. That is not a gap in coverage but the design:
every test outside `tests/live/` reasons about recorded entry shapes, so CI
proves the accounting and says nothing about whether the marker set still
matches the `claude` you have installed. Only a live run answers that — see
[Before you tag](#before-you-tag).

## Cutting a release

1. **Run the live tests.** They spend real quota and take a minute or two, and
   they are the only check that reads what `claude` actually writes:

   ```bash
   AGENTPROXY_RUN_LIVE=1 pytest -m live
   ```

2. **Bump the version** in the two places the release job compares — they must
   match the tag or the job stops before building:

   - `pyproject.toml` → `[project] version`
   - `src/agentproxy/__init__.py` → `__version__`

3. **Commit** the bump:

   ```bash
   git commit -am "Release v0.1.0"
   ```

4. **Tag and push** — the tag is `v` + the version:

   ```bash
   git tag v0.1.0
   git push origin master
   git push origin v0.1.0
   ```

5. The Release workflow publishes to
   `https://github.com/ThinkerYzu/agent-proxy/releases`. If nothing appears,
   the Actions tab has the reason.

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
pip install 'git+https://github.com/ThinkerYzu/agent-proxy@v0.1.0'
```

which puts an `agent-proxy` binary on the path — the only thing
[r2d2box](https://github.com/ThinkerYzu/r2d2box) and
[agent-desktop-env](https://github.com/ThinkerYzu/agent-desktop-env) need from
this repo. The Release page also carries the sdist and wheel for anyone
installing without git.

Publishing to PyPI would need a decision this project has not made: the package
is useless without a `claude` binary it cannot declare as a dependency. If it
ever happens, configure Trusted Publishing for the repo and add a job to
`release.yml` — memscout's `RELEASING.md` sketches one.
