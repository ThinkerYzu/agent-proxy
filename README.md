# agent-proxy

A stdio wrapper around an interactive `claude` TUI that **states** turn
boundaries instead of leaving clients to infer them. It replaces
[claudejson](../claudejson/), inheriting its pty and transcript machinery and
replacing its turn accounting.

Design docs live in `proj_docs/agent-proxy/` — [SPEC](../../proj_docs/agent-proxy/SPEC.md)
for what and why, [DESIGN](../../proj_docs/agent-proxy/DESIGN.md) for how,
[IMPLEMENTATION-GUIDE](../../proj_docs/agent-proxy/IMPLEMENTATION-GUIDE.md) for
the code-level plan.

## Status

Phase 1. `TurnTracker` is complete and verified against a recorded session;
there is no live wrapping yet, so `agent-proxy` itself does not run. What
works today is replay:

```bash
python3 -m agentproxy.replay tests/fixtures/synthetic-session.jsonl --assert-drained
```

## What it emits

Line-delimited JSON. Every message carries `seq`, its `type`, all three
outstanding counts, and — when it belongs to a turn — that turn's id and kind:

```json
{"seq": 41, "type": "turn_start", "turn": {"id": "t-6", "kind": "unowned"},
 "outstanding": {"user": 0, "unowned": 1, "background": 0}, "caused_by": "bsynthimport"}
```

That one line is the project in miniature: a turn nobody prompted, named, and
tied to the background command whose completion woke it.

## Layout

| Path | What it is |
|---|---|
| `src/agentproxy/turn_tracker.py` | the turn table, the task table, binding and end-of-turn |
| `src/agentproxy/protocol.py` | the envelope and the stdout writer |
| `src/agentproxy/replay.py` | replay a recorded transcript, with no live `claude` |
| `src/agentproxy/{pty_driver,io_loop,screen_model,input_encoder,transcript_tailer,ids,log}.py` | inherited from claudejson |
| `tools/make_synthetic_fixture.py` | build the test session out of real entry shapes |
| `tools/capture_fixture.py` | cut a fixture out of a real session transcript |

## Building and testing

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

Tests need no `claude` binary. The `live` and `slow` markers are opt-in, as in
claudejson.

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
