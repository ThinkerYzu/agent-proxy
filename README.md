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
python3 -m agentproxy.replay tests/fixtures/session-08ff12b4.jsonl --assert-drained
```

## What it emits

Line-delimited JSON. Every message carries `seq`, its `type`, all three
outstanding counts, and — when it belongs to a turn — that turn's id and kind:

```json
{"seq": 736, "type": "turn_start", "turn": {"id": "t-23", "kind": "unowned"},
 "outstanding": {"user": 0, "unowned": 1, "background": 0}, "caused_by": "by3c686m9"}
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

`tests/fixtures/session-08ff12b4.jsonl` is a real 1187-entry session, with long
strings truncated so it fits in the repository. It was picked because it
contains all three background-completion shapes and three `/compact` runs. Its
sidecar, `session-08ff12b4.prompts.json`, names which entries were prompts a
client typed — provenance a recording cannot carry on its own.

Regenerate either with:

```bash
python3 tools/capture_fixture.py ~/.claude/projects/<dir>/<session>.jsonl tests/fixtures/<name>.jsonl
```
