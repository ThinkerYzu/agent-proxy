# agent-proxy

A stdio wrapper around an interactive `claude` TUI that **states** turn
boundaries instead of leaving clients to infer them. It replaces claudejson, an
earlier unpublished wrapper, inheriting its pty and transcript machinery and
replacing its turn accounting.

[API.md](API.md) is the wire contract — every command, every message, and what
a client may assume about the order. Read it first if you are writing a client.

The design documents that argue *why* the protocol has this shape are not
published with the code. What is here is meant to stand on its own: API.md for
the contract, [RELEASING.md](RELEASING.md) for cutting a version, and the
module docstrings for the reasoning — `turn_tracker.py` on what moves a turn,
`fallback.py` on why a marker alone cannot be relied on to end one.

## Status

Phase 2. It runs:

```bash
echo '{"type":"submit","text":"Reply with exactly: PONG"}' | agent-proxy
```

```json
{"seq": 1, "type": "ready", "outstanding": {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0, "orphan": 0}, "session_id": "…"}
{"seq": 2, "type": "ack", "turn": {"id": "t-1", "kind": "user"}, "outstanding": {"user": 1, "unowned": 0, "background": 0, "monitor": 0, "agent": 0, "orphan": 0}}
{"seq": 3, "type": "turn_start", "turn": {"id": "t-1", "kind": "user"}, "outstanding": {"user": 1, "unowned": 0, "background": 0, "monitor": 0, "agent": 0, "orphan": 0}}
{"seq": 4, "type": "text", "turn": {"id": "t-1", "kind": "user"}, "outstanding": {"user": 1, "unowned": 0, "background": 0, "monitor": 0, "agent": 0, "orphan": 0}, "text": "PONG"}
{"seq": 5, "type": "turn_end", "turn": {"id": "t-1", "kind": "user"}, "outstanding": {"user": 0, "unowned": 0, "background": 0, "monitor": 0, "agent": 0, "orphan": 0}, "basis": "marker:turn_duration", "outcome": "success", "usage": {"duration_ms": 1670, "output_tokens": 5}}
```

Unrecognised arguments go through to the inner `claude`, so `agent-proxy
--model opus` works as expected. Set `AGENTPROXY_LOG=/path/to/file` to record
the pty bytes, the stdin lines and the state transitions behind the stream.

The session flags are the exception — the proxy resolves those itself so it
can name the session before it types anything. `ready` is always the first
message and always carries a real `session_id`, including for `--continue`,
a bare `--resume`, and `--fork-session`. Keep that id and `agent-proxy
--resume <id>` picks the conversation up in a later process; see
[API.md](API.md#sessions) for the full table.

Every requirement, constraint and success criterion that can be checked
without a client has a test naming it, in `tests/requirements/`.

Both clients have migrated — [r2d2box](https://github.com/ThinkerYzu/r2d2box),
which [agent-desktop-env](https://github.com/ThinkerYzu/agent-desktop-env)
mounts, and a second client that is not public — so claudejson is retired.

## Installing

Not on PyPI: the package is useless without a `claude` binary it cannot declare
as a dependency. Install from a release tag, which puts an `agent-proxy` on the
path — the only thing a client needs from this repo:

```bash
pip install 'git+https://github.com/ThinkerYzu/agent-proxy@v0.1.0'
```

[RELEASING.md](RELEASING.md) covers cutting one.

## Commands

| What you want | Command |
|---|---|
| a prompt answered | `{"type": "submit", "text": "…"}` on stdin |
| authoritative state, any time | `{"type": "status"}` on stdin |
| a turn taken back — dropped if still queued, interrupted with `Esc` if running | `{"type": "cancel", "turn": "t-6"}` on stdin; the turn ends `outcome: "cancelled"` once the inner is seen to stop |
| to know which `ack` is yours | add a `"ref"` of your own to either — it comes back on the answer |
| the tracker against a recorded session, no `claude` | `python3 -m agentproxy.replay tests/fixtures/synthetic-session.jsonl --assert-drained` |

## What it emits

Line-delimited JSON. Every message carries `seq`, its `type`, every
outstanding count, and — when it belongs to a turn — that turn's id and kind:

```json
{"seq": 41, "type": "turn_start", "turn": {"id": "t-6", "kind": "unowned"},
 "outstanding": {"user": 0, "unowned": 1, "background": 0, "monitor": 0, "agent": 0, "orphan": 0}, "caused_by": "bsynthimport"}
```

That one line is the project in miniature: a turn nobody prompted, named, and
tied to the background command whose completion woke it.

## Layout

| Path | What it is |
|---|---|
| `API.md` | the stdio protocol reference for clients |
| `RELEASING.md` | how a release is cut, and what a version number promises |
| `.github/workflows/` | CI on every push, a GitHub Release on every `v*` tag |
| `src/agentproxy/turn_tracker.py` | the turn table, the task table, binding and end-of-turn; fed by transcript entries, or by stated facts for an agent that reports its own boundaries |
| `src/agentproxy/protocol.py` | the envelope and the stdout writer |
| `src/agentproxy/commands.py` | the stdin command parser, the same for every inner agent |
| `src/agentproxy/proxy.py` | the wiring — every decision, no process IO |
| `src/agentproxy/cli.py` | the process boundary — pty, transcript, stdout, signals |
| `src/agentproxy/liveness.py` | idle or active, settled, and the two dialogs that block input |
| `src/agentproxy/fallback.py` | ending a turn that no marker closed |
| `src/agentproxy/submit_queue.py` | accepted prompts, typed one at a time |
| `src/agentproxy/inner_claude.py` | argv, env and session id for the inner spawn |
| `src/agentproxy/orphans.py` | processes the inner lost track of: the subreaper flag, adoption, reaping, the `orphan` count |
| `src/agentproxy/replay.py` | replay a recorded transcript, with no live `claude` |
| `src/agentproxy/{pty_driver,io_loop,screen_model,input_encoder,transcript_tailer,ids,log}.py` | inherited from claudejson |
| `tools/make_synthetic_fixture.py` | build the test session out of real entry shapes |
| `tools/capture_fixture.py` | cut a fixture out of a real session transcript |
| `tests/harness.py` | a proxied session with the pty taken out, shared by the suites below |
| `tests/requirements/` | one test group per SPEC requirement, constraint and success criterion |
| `tests/requirements/reference_client.py` | a client with no inference layer, used to check the protocol keeps its word |

## Building and testing

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

287 tests, none of which needs a `claude` binary (one of them proves the
subreaper mechanism on the real kernel, in a subprocess). Seven more do, and
are opt-in twice over — a marker alone deselects nothing on a bare `pytest`, and these
spend real quota:

```bash
AGENTPROXY_RUN_LIVE=1 pytest -q -m live
```

Worth running when `claude` changes version: it is the only test that reads
the entry shapes the installed binary actually writes, rather than the ones
the fixtures model.

## Fixtures

`tests/fixtures/synthetic-session.jsonl` is a 93-entry session assembled by
`tools/make_synthetic_fixture.py`. Its conversation is invented; every entry
*shape* in it — envelope fields, content blocks, marker subtypes, the two
background-launch wordings, the `queue-operation` pairs, the cluster
`/compact` writes after its boundary — was copied from a real recorded
session. The tracker reads only the shapes, so nothing is lost, and the
repository carries no transcript of real work.

Its sidecar, `synthetic-session.prompts.json`, names which entries were
prompts a client typed — provenance a recording cannot carry on its own.

Regenerate with:

```bash
python3 tools/make_synthetic_fixture.py
```

The output is byte-stable, so a regeneration shows up as a reviewable diff and
a test fails if the committed fixture drifts from the builder.

To check the tracker against a real session on your own machine — worth doing
when `claude` changes shape, and not something to commit:

```bash
python3 tools/capture_fixture.py ~/.claude/projects/<dir>/<session>.jsonl /tmp/<name>.jsonl
python3 -m agentproxy.replay /tmp/<name>.jsonl --quiet --assert-drained
```
