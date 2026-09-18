# agent-proxy API

The wire contract between a client and `agent-proxy`. Everything a client can
say, everything the proxy can say back, and what it is allowed to assume about
the order.

This is the reference. Where it and the code disagree, the code is
`src/agentproxy/protocol.py` and `src/agentproxy/turn_tracker.py`; the
reasoning behind each rule is in the module docstrings, `turn_tracker.py` for
what moves a turn and `fallback.py` for what ends one no marker ended.

## Invocation

```bash
agent-proxy [claude arguments...]
```

The proxy spawns one interactive `claude` on a pty, reads line-delimited JSON
commands on stdin, and writes line-delimited JSON messages on stdout. One
client per process; the client owns the process's lifetime.

Arguments the proxy does not recognise go to the inner `claude`, so
`agent-proxy --resume 4f0d… --model opus` works. Four rules apply to that
passthrough:

| Rule | Effect |
|---|---|
| print-mode flags dropped | `-p`, `--print`, `--verbose`, `--input-format`, `--output-format` are removed — they would turn off the TUI the design depends on |
| permissions skipped | `--dangerously-skip-permissions` is added unless the caller passed it |
| one tool denied | `AskUserQuestion` is folded into `--disallowedTools`, ahead of any `--`; it waits on a human keystroke no client can send |
| session flags rewritten | the proxy resolves the session itself and passes a canonical `--session-id` / `--resume` pair, replacing whatever session flags the caller wrote |

### Sessions

Every invocation resolves to a session id, and `ready` — always the first
message — carries it. It is never null. Record it and a later process can
resume the same conversation with `--resume <id>`; that is the only piece of
state that outlives the proxy.

| You run | You get | The transcript is read |
|---|---|---|
| `agent-proxy` | a new session, id minted for you | from the start |
| `agent-proxy --session-id <uuid>` | that session | from the start |
| `agent-proxy --resume <uuid>` | that session, continued | from the end — its existing turns are not replayed |
| `agent-proxy --continue` | the newest session in this directory | from the end |
| `agent-proxy --resume` (no id) | the same; the proxy resolves it rather than opening the inner's picker, which it could not answer | from the end |
| `agent-proxy --resume <uuid> --fork-session` | a **new** session branched off that one; `ready` names the fork, not the original | skipping the copy of the original's history that the fork is backfilled with |

Two things worth knowing:

- **`--continue` and a bare `--resume` pick the newest transcript in the
  directory**, and the proxy then passes `--resume <id>` explicitly — so the id
  you are told is the session the inner actually runs, not a guess about what
  it chose. If the directory has no sessions yet, you get a new one, which is
  what the inner does there too.
- **`--session-id` cannot be combined with `--resume` or `--continue`** unless
  `--fork-session` is also given. The inner rejects that combination, so the
  proxy rejects it first: a single `error` message, exit status 2, and no
  session started. That is the only case where `ready` is not the first thing
  you read.

### Environment

| Variable | Default | What it does |
|---|---|---|
| `AGENTPROXY_LOG` | unset | path to a debug log of pty bytes, stdin lines and state transitions |
| `AGENTPROXY_FALLBACK_IDLE_S` | `8` | footer idle + transcript quiet for this long ends a turn |
| `AGENTPROXY_FALLBACK_QUIET_S` | `60` | pty silent + transcript quiet for this long ends a turn |
| `AGENTPROXY_FALLBACK_STALL_S` | `1800` | the ceiling — transcript quiet this long ends a turn regardless |
| `AGENTPROXY_FALLBACK_UNMATCHED_S` | `60` | a typed prompt unrecognised this long, with nothing running, ends |
| `AGENTPROXY_SETTLE_QUIESCENCE_S` | `0.5` | how long to wait after the first idle frame before typing |
| `AGENTPROXY_DIALOG_ANSWER_DELAY_S` | `0.15` | pause before each key the proxy types that is not a prompt: a dialog answer, a `cancel`'s `Esc`, the composer clear |
| `AGENTPROXY_KILL_ORPHANS` | unset | `1` kills adopted orphan processes when the proxy exits (SIGTERM, 2 s, SIGKILL); unset leaves them running and reports them `abandoned` — see [Orphaned processes](#orphaned-processes) |
| `CLAUDEJSON_SEND_QUIESCENCE_S` | `0.7` | pty quiet required before keystrokes go out (inherited name) |
| `CLAUDEJSON_RESUBMIT_QUIESCENCE_S` | `1.5` | pty quiet required before re-sending a submit CR (inherited name) |

The proxy sets `CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1` on the inner. Without
it a `claude` launched from inside another Claude Code session inherits
`CLAUDE_CODE_CHILD_SESSION`, writes no transcript, and the proxy goes blind.

## The stream

Every line on stdout is one complete JSON object with the same envelope:

```json
{"seq": 42, "type": "text", "turn": {"id": "t-7", "kind": "user"},
 "outstanding": {"user": 1, "unowned": 0, "background": 2, "monitor": 0, "agent": 0, "orphan": 0},
 "text": "The stale search turned up a glib checkout I didn't use."}
```

| Field | Present | Meaning |
|---|---|---|
| `seq` | always | counts from 1, no gaps; a jump means the client dropped a line |
| `type` | always | one of the message types below |
| `outstanding` | always | every count at the moment of emission |
| `turn` | messages belonging to a turn | `id` correlates, `kind` is `user` or `unowned` |
| `ref` | messages answering a command that carried one | the client's own token, echoed verbatim |

`outstanding` is absolute, not a delta. A client never accumulates it, and a
client that missed messages is correct again on the next one it sees.

**Read the counts; do not derive them from the events.** All the messages one
transcript entry produces are stamped after that entry has been folded into
the proxy's tables, so inside such a batch the counts run ahead of the events
you have read — a `task_start` emitted alongside the `task_end` that closes it
already carries the count the pair leaves behind. The counts are never stale,
only early, and the next message restates them.

| Count | Counts | Falls when |
|---|---|---|
| `user` | prompts this client submitted, queued and running | that turn's `turn_end` |
| `unowned` | turns the inner `claude` started on its own | that turn's `turn_end` |
| `background` | background shell commands still running | that task's `task_end` |
| `monitor` | monitors still watching | that task's `task_end` |
| `agent` | subagents still working | that task's `task_end` |
| `orphan` | processes the inner lost track of, now the proxy's children; see [Orphaned processes](#orphaned-processes) | that process's `orphan_end`. Present only on Linux; absent, not `0`, elsewhere |

**Every count is zero exactly when nothing of its kind is running, and no
work is outside them.** So `any(outstanding.values())` is the whole answer to
"is anything still running" — including for a count added after your client
was written. Do not hard-code the key list: read the values. A task whose
launch was never recognised counts under `background`, which is the likeliest
thing an unrecognised launch is. The turn and task counts are disjoint and
summable; `orphan` counts *processes* rather than tasks, so add it to nothing.
Test it.

A background task is not a turn. One that finishes while the agent is idle
starts an unowned turn; one that finishes mid-turn is folded into the running
turn and starts nothing. That is why the count is separate.

The three background counts correspond to `task.kind` on both task messages:

| `kind` | What it is |
|---|---|
| `shell` | a `run_in_background` command, or a foreground one promoted on timeout |
| `monitor` | a watch on a log, a file or a socket, emitting events until stopped |
| `agent` | a subagent launched to run on its own |
| `unknown` | the launch was never seen; the task was registered from a notification |

They are counted separately because they mean different things to a person
looking at a UI, and because they live for different lengths of time: a shell
command ends when it exits, while a persistent monitor holds `monitor` above
zero until something stops it, which is the truth about it rather than a fault.
`kind` on the task messages says the same thing per task that the counts say in
aggregate — use whichever suits.

## Commands

Write one JSON object per line to stdin and flush. Three are understood:

```json
{"type": "submit", "text": "what does load_timeout_id do?", "ref": "c-17"}
{"type": "status", "ref": "c-18"}
{"type": "cancel", "turn": "t-6", "ref": "c-19"}
```

`submit` is answered by an `ack` before anything reaches the inner `claude` —
it never waits on the pty. `text` must be a non-empty string. `status` is
answered by a `state` message, also without touching the keyboard. `cancel`
names a turn by the id its `ack` carried and is answered by an `ack` naming
that same turn — see [Cancelling a turn](#cancelling-a-turn).

A line that is not JSON, not an object, has an unknown `type`, is a `submit`
with no usable `text`, or a `cancel` naming a turn that cannot be cancelled,
produces an `error` message and nothing else. The session continues.

### Correlating a command with its answer

`ref` is optional, opaque to the proxy, and echoed verbatim on the message
answering that command — the `ack`, the `state`, or the `error` for a command
the proxy rejects. **Use it.** The answer is not the next line you read: the
stream is also carrying turn messages, so an `ack` routinely arrives two or ten
lines after the `submit` that earned it.

```
{"seq": 40, "type": "text",  "turn": {"id": "t-5", "kind": "unowned"}, ...}
{"seq": 41, "type": "ack",   "turn": {"id": "t-6", "kind": "user"}, ..., "ref": "c-17"}
```

Without `ref` the only correspondence left is positional — the Nth `ack`
answers the Nth `submit` — and that is the ordinal matching this protocol
exists to remove, at the one point where a client *acquires* the id it will
later match by. It breaks on the first rejection, because a rejected `submit`
produces an `error` and no `ack`:

```
{"type":"submit","text":"fix the leak","ref":"c-1"}  ->  ack   t-1  ref c-1
{"type":"submit","text":"   ","ref":"c-2"}           ->  error      ref c-2
{"type":"submit","text":"now test it","ref":"c-3"}   ->  ack   t-2  ref c-3
```

A client counting acks binds `t-2` to its second prompt and then waits forever
on a turn belonging to the third. Every later prompt is off by one, silently.

A command that arrives as an unparseable line has no `ref` the proxy can read,
so that one `error` is unattributable by construction. Everything else is.

### Cancelling a turn

`cancel` takes the turn back, as far as that is possible, and reports what
actually happened rather than what was asked:

| The turn is | What the proxy does | How it ends |
|---|---|---|
| queued, not yet typed | drops it from the queue | at once, in the same batch as the `ack`: `basis: "cancel:withdrawn"`, `outcome: "cancelled"` |
| running | sends one `Esc` to the inner, as a person at the keyboard would | when the inner is *seen* to stop — see below |
| typed but not yet in the transcript | holds the request and sends the `Esc` the moment the turn binds | as running |
| already ended, unknown, or an unowned turn | nothing: `error` | — |

A running turn stops in one of two shapes, and which one is the inner's
choice, not the proxy's:

- **The inner writes an interruption entry.** When the model had already
  produced anything, the transcript gets a `[Request interrupted by user]`
  entry — preceded, if a tool was being run, by a `tool_result` declining it
  ("The user doesn't want to proceed with this tool use…") that belongs to
  the turn. The turn ends `basis: "marker:interrupted"`, `outcome:
  "cancelled"`. Aborting a tool can take the inner several seconds; the
  `turn_end` waits for it.
- **The inner hands the prompt back.** When the model had produced nothing
  yet, the inner treats the `Esc` as an un-send: it writes nothing and puts
  the prompt back in its composer for editing. The proxy recognises its own
  prompt sitting there on an idle frame, ends the turn `basis:
  "cancel:restored"`, `outcome: "cancelled"`, and clears the composer before
  it types anything else — a prompt typed on top of the leftover text would
  be submitted as one with it.

What a client may rely on:

1. The `ack` means the request was taken, not that the turn is over. Wait on
   the `turn_end` for that id, as for any turn; it carries the `ref`-less
   ending, the `ack` carries the `ref`.
2. `outcome: "cancelled"` appears only on a `turn_end` whose `basis` is one of
   the three above. An `Esc` the inner did not act on — it happens; a single
   byte on a busy pty — ends the turn however it really ends, with
   `outcome: "success"` and the real `basis`, and a second `cancel` for the
   same turn is acknowledged but sends nothing more.
3. Only the named turn is affected. Prompts queued behind it still run;
   cancel each id you want gone. Background tasks are untouched — `Esc`
   does not stop them, and a task the cancelled turn launched runs to its
   `task_end` and may wake an unowned turn afterwards, exactly as it would
   have.
4. Unowned turns cannot be cancelled. The handed-back shape is recognised by
   the text the proxy typed, which an unowned turn does not have.

## Message types

| `type` | When | Fields beyond the envelope |
|---|---|---|
| `ready` | first message of the session, always `seq` 1 | `session_id` — always a real id, see [Sessions](#sessions) |
| `ack` | a `submit` or a `cancel` was accepted | `ref` if the command carried one (`turn` carries the new id, or the id being cancelled) |
| `turn_start` | a turn began running | `caused_by` when a background task woke it |
| `text` | assistant prose | `text` |
| `thinking` | assistant reasoning | `text` |
| `tool_use` | the assistant called a tool | `tool`, `input`, `tool_use_id` |
| `tool_result` | a tool returned | `tool_use_id`, `content`, `is_error` |
| `turn_end` | a turn finished | `basis`, `outcome`, `usage` |
| `task_start` | background work was launched | `task` (`{id, kind}`), `summary` (string or null), `command` (string or null) |
| `task_end` | background work ended | `task` (`{id, kind}`), `status`, `exit_code` (int or null), `output` (string or null) |
| `orphan_start` | a process was reparented to the proxy | `process` (`{pid, command}`); see [Orphaned processes](#orphaned-processes) |
| `orphan_end` | an adopted process ended, or the proxy stopped watching it | `process`, `status` (`exited`, `killed`, `abandoned`), `exit_code` (int or null), `signal` (int or null) |
| `state` | reply to `status` | `turns`, `tasks`, `orphans` (Linux only), `ref` if the query carried one |
| `error` | a rejected command, or a session failure | `error` (string), `ref` if a rejected command carried one |

Notes on the fields that carry weight:

- **`turn_end.basis`** says how the end was decided — see [End of
  turn](#end-of-turn).
- **`turn_end.outcome`** is `success`, `error` or `cancelled`. `error` means
  the turn was closed by the failure path, not that the agent's answer was
  wrong; `cancelled` means a `cancel` took effect — see [Cancelling a
  turn](#cancelling-a-turn).
- **`turn_end.usage`** holds `duration_ms` (only from a marker that carried
  one) and `output_tokens` (only when non-zero). It can be `{}`.
- **`task_end.status`** is `completed`, `failed`, `killed` or `stopped` — how
  the work ended, which `exit_code` alone cannot tell you and which is often
  absent for anything but a shell command. Every one of the four ends the task
  and drops the count; do not treat `completed` as the only ending.
- **`task_start.command`** is the shell text for a `shell` task (Bash's own
  `command` input, the same source `summary` reads `description` from) and
  `null` for a `monitor` or `agent` task, which has no single command.
- **`task_end.output`** is the last 256 characters of the task's own output
  file, or `null` when there is none to read (a monitor or agent task, or a
  shell task whose file the proxy could not read). A preview for the client
  to show, not the log — there is no way to ask for more of it, and no
  mid-flight read while the task is still running.
- **`turn_start.caused_by`** is a background task id, tying an unowned turn to
  the work whose completion woke it. It always names a task the client has
  already seen a `task_start` for.
- **`tool_result`** is the one turn message that can arrive with no `turn`
  field, if the transcript runs ahead of the proxy's turn accounting.
- **`error`** carries no `turn`. When a failure kills turns, the `error` comes
  first and one `turn_end` per killed turn follows it.

`state` answers with everything unfinished, and depends on nothing the client
has seen before:

```json
{"seq": 88, "type": "state",
 "outstanding": {"user": 1, "unowned": 0, "background": 1, "monitor": 0, "agent": 0, "orphan": 1},
 "turns": [{"id": "t-4", "kind": "user", "state": "queued", "caused_by": null}],
 "tasks": [{"id": "bunha2ebm", "kind": "shell", "summary": "build firefox", "command": "./mach build"}],
 "orphans": [{"pid": 28457, "command": "tail -f /dev/null"}]}
```

Give the `status` a `ref` and the `state` carries it back; two queries in
flight then cannot be confused.

It lists turns, not the `ref`s that claimed them, so **keep your own ref-to-id
map across a reconnect**. A turn id in the reply can be recognised as one you
were already waiting on; it cannot be re-attached afterwards to a prompt whose
`ack` you never read.

## Orphaned processes

A process the model starts can outlive the shell that started it: `cmd &`
with the shell returning at once, a daemon that double-forks, or everything
left behind if the inner `claude` dies. The inner's own task tracking has
lost it by then, so no `task_end` will ever mention it, and without this
section the counts would read zero while it ran on.

On Linux the proxy makes itself the *subreaper* of its process tree
(`prctl(PR_SET_CHILD_SUBREAPER)`), so every such process is reparented to the
proxy instead of to init. The proxy then reports them like tasks:

- `orphan_start` when one appears, with its `pid` and `command`, the process's
  own command line, cut to 256 characters, or `null` if it had already exited
  by the time the proxy looked.
- `orphan_end` when it is reaped, with `status: "exited"` and its `exit_code`,
  or `status: "killed"` and the `signal` that ended it.
- `outstanding.orphan`, the number alive right now, on every message.
- `orphans` in the `state` reply, listing the ones alive.

Three things to expect:

- **The inner orphans processes of its own.** Its first Bash call in a
  session spawns a `/bin/bash -c -l` that writes a shell snapshot and is
  orphaned for a moment; you will see an `orphan_start`/`orphan_end` pair for
  it inside the turn. That is the truth about the process tree, not a fault.
- **An `orphan_start` can precede the `tool_use` that caused it.** The process
  tree is read directly and the transcript lags, so the process is often seen
  before the tool call that spawned it is.
- **At shutdown the survivors are `abandoned` by default**: reported with
  `orphan_end` `status: "abandoned"` and left running, since the proxy cannot
  tell a leak from a server the model detached on purpose. Set
  `AGENTPROXY_KILL_ORPHANS=1` to have them terminated instead (SIGTERM, a 2 s
  grace, then SIGKILL), each reported with its real ending. Either way every
  `orphan_start` gets its `orphan_end` and the final counts are zero.

Only a process whose parent has died is counted. A grandchild whose parent is
still running belongs to that parent's task and is covered by `background`;
it moves to `orphan` on its own the moment the parent exits.

Off Linux, or where the `prctl` fails, the proxy runs as before: no `orphan`
key on any message, no `orphans` in `state`, and nothing adopted.

## The shape of one turn

<svg viewBox="0 0 720 300" style="max-width:100%;height:auto" xmlns="http://www.w3.org/2000/svg">
<rect x="0" y="0" width="720" height="300" fill="#ffffff"/>
<defs>
<marker id="api-a" markerWidth="9" markerHeight="7" refX="8" refY="3.5" orient="auto">
<polygon points="0 0, 9 3.5, 0 7" fill="#334155"/>
</marker>
</defs>
<rect x="20" y="14" width="120" height="30" rx="5" fill="#dbeafe" stroke="#2563eb" stroke-width="1.4"/>
<text x="80" y="34" font-family="sans-serif" font-size="12" font-weight="bold" fill="#1e3a8a" text-anchor="middle">client</text>
<rect x="300" y="14" width="120" height="30" rx="5" fill="#f1f5f9" stroke="#64748b" stroke-width="1.4"/>
<text x="360" y="34" font-family="sans-serif" font-size="12" font-weight="bold" fill="#0f172a" text-anchor="middle">agent-proxy</text>
<rect x="580" y="14" width="120" height="30" rx="5" fill="#fef3c7" stroke="#d97706" stroke-width="1.4"/>
<text x="640" y="34" font-family="sans-serif" font-size="12" font-weight="bold" fill="#78350f" text-anchor="middle">inner claude</text>
<line x1="80" y1="44" x2="80" y2="288" stroke="#94a3b8" stroke-width="1"/>
<line x1="360" y1="44" x2="360" y2="288" stroke="#94a3b8" stroke-width="1"/>
<line x1="640" y1="44" x2="640" y2="288" stroke="#94a3b8" stroke-width="1"/>
<line x1="80" y1="72" x2="356" y2="72" stroke="#334155" stroke-width="1.4" marker-end="url(#api-a)"/>
<text x="86" y="66" font-family="sans-serif" font-size="10.5" fill="#334155">submit "…"</text>
<line x1="360" y1="100" x2="84" y2="100" stroke="#334155" stroke-width="1.4" marker-end="url(#api-a)"/>
<text x="94" y="94" font-family="sans-serif" font-size="10.5" fill="#334155">ack — turn t-1, user 1 (before anything is typed)</text>
<line x1="360" y1="132" x2="636" y2="132" stroke="#334155" stroke-width="1.4" marker-end="url(#api-a)"/>
<text x="366" y="126" font-family="sans-serif" font-size="10.5" fill="#334155">keystrokes, when idle and nothing running</text>
<line x1="640" y1="164" x2="364" y2="164" stroke="#334155" stroke-width="1.4" marker-end="url(#api-a)"/>
<text x="374" y="158" font-family="sans-serif" font-size="10.5" fill="#334155">transcript: the prompt, written as an entry</text>
<line x1="360" y1="192" x2="84" y2="192" stroke="#334155" stroke-width="1.4" marker-end="url(#api-a)"/>
<text x="94" y="186" font-family="sans-serif" font-size="10.5" fill="#334155">turn_start — t-1 matched the prompt we typed</text>
<line x1="640" y1="224" x2="364" y2="224" stroke="#334155" stroke-width="1.4" marker-end="url(#api-a)"/>
<text x="374" y="218" font-family="sans-serif" font-size="10.5" fill="#334155">transcript: blocks, then turn_duration</text>
<line x1="360" y1="256" x2="84" y2="256" stroke="#334155" stroke-width="1.4" marker-end="url(#api-a)"/>
<text x="94" y="250" font-family="sans-serif" font-size="10.5" fill="#334155">text / tool_use / tool_result, then turn_end — user 0</text>
<text x="94" y="278" font-family="sans-serif" font-size="10" fill="#64748b">an unowned turn is the same from turn_start onwards, with no ack</text>
</svg>

What a client may rely on:

1. `ready` is `seq` 1.
2. `ack` arrives on receipt of the `submit`, bounded by the proxy's own
   processing and not by the inner `claude` — though other messages may
   already be ahead of it in the stream, which is what `ref` is for. The turn
   counts as outstanding from that moment, while it is still queued.
3. A user turn's `turn_start` arrives only once its prompt lands in the
   transcript, which can be much later — the proxy types one prompt at a time.
4. An unowned turn has no `ack`: `turn_start` is the first you hear of it.
5. Every turn that starts also ends. A turn can also end *without* having
   started, when the session fails while it is still queued.
6. At most one turn runs at a time, so no message is ambiguous about which
   turn it belongs to.

**Wait on `turn.id`, never on order or counts.** A client's prompt can sit
behind an unowned turn and behind its own earlier prompt, so the `turn_end`
before yours is routinely someone else's. Ignoring foreign `turn_end` messages
needs no bookkeeping — match the id and drop the rest. Counters answer a
different question: *is anything still outstanding*, for gating the composer.

## End of turn

`basis` names the evidence the proxy used. Tier 1 is a transcript marker;
tier 2 is the fallback that makes "a turn hangs forever" impossible.

| `basis` | Tier | Condition |
|---|---|---|
| `marker:turn_duration` | 1 | the ordinary end-of-turn marker |
| `marker:compact_boundary` | 1 | `/compact` completed |
| `marker:interrupted` | 1 | the inner wrote its interruption entry after a `cancel`'s `Esc` |
| `cancel:restored` | — | after a `cancel`'s `Esc`, the inner handed the prompt back to its composer and wrote nothing |
| `cancel:withdrawn` | — | a `cancel` dropped the turn from the queue before it was typed |
| `fallback:idle` | 2 | TUI footer idle and transcript quiet for `AGENTPROXY_FALLBACK_IDLE_S` |
| `fallback:quiet` | 2 | pty silent and transcript quiet for `AGENTPROXY_FALLBACK_QUIET_S`, whatever the footer says |
| `fallback:stalled` | 2 | transcript quiet for `AGENTPROXY_FALLBACK_STALL_S`, nothing else considered |
| `fallback:unmatched` | 2 | the prompt was typed but never appeared in the transcript, for `AGENTPROXY_FALLBACK_UNMATCHED_S`, with nothing running |
| `error` | — | the turn was killed by a failure |

A client can treat a `fallback:*` end as slightly less certain, but the more
useful reading is operational: the marker set is open, so a `fallback:*` rate
above roughly zero means `claude` grew a turn shape worth teaching to the
proxy. `fallback:stalled` should be vanishingly rare — it can fire on a turn
that is genuinely alive, such as one running a single long foreground command.

`fallback:unmatched` is the one that says something went wrong on the way *in*
rather than on the way out. A turn ending this way was acknowledged and typed,
and the TUI then altered or dropped it, so the entry that would have bound it
never arrived — nothing the client did, and nothing it can retry differently.
Whatever the inner `claude` did in the meantime is reported, correctly, as an
`unowned` turn. Any rate above zero is worth chasing: it means prompts are
reaching `claude` in a different form from the one the client sent.

## Failure and shutdown

When the inner `claude` dies or the proxy hits an exception, it emits an
`error` and then closes everything it opened: the running turn and every turn
queued behind it get a `turn_end` with `basis: "error"` and
`outcome: "error"`, every unfinished background task gets a `task_end`
with `"exit_code": null`, and every adopted orphan process gets an
`orphan_end` ([abandoned or killed](#orphaned-processes)). No counter is left
above zero and no client is left waiting on an id that will never complete.

Closing stdin is the clean shutdown. The proxy finishes the turn in flight,
then exits. A background task or an orphan process still running does
**not** hold the session open — the client that would hear about it has
already gone — so that case ends with `"error": "client closed stdin with
background work still running"` followed by the closing messages. Other reasons appear in the same field:

| `error` text | Cause |
|---|---|
| `inner claude exited` | the pty reached EOF |
| `interrupted by signal` | SIGTERM or SIGINT |
| `proxy failed: …` | an exception, re-raised after the message |
| `client closed stdin mid-turn` | reserved — `cli.py` passes its own stop condition, so the loop waits for the turn instead |
| `inner claude went silent` | reserved — the io loop's idle budget is not armed today |

`agent-proxy` exits 0 for every outcome it handles itself; an unhandled
exception exits non-zero after the `error` message is written.

## A minimal client

```python
import json, subprocess

proxy = subprocess.Popen(
    ["agent-proxy"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
)

command = {"type": "submit", "text": "Reply PONG", "ref": "c-1"}
proxy.stdin.write(json.dumps(command) + "\n")
proxy.stdin.flush()

mine = None
for line in proxy.stdout:
    message = json.loads(line)
    if message["type"] == "ack" and message.get("ref") == "c-1":
        mine = message["turn"]["id"]
    elif message["type"] == "error" and message.get("ref") == "c-1":
        break                      # rejected — no turn is coming
    elif message["type"] == "text" and message.get("turn", {}).get("id") == mine:
        print(message["text"])
    elif message["type"] == "turn_end" and message["turn"]["id"] == mine:
        break                      # our turn, whatever else ended meanwhile

    busy = any(message["outstanding"].values())   # gate the composer on this
```

Three habits worth keeping from that sketch: the turn id is claimed by `ref`,
the wait resolves on `turn.id`, and the composer is gated on `outstanding`.
Everything else — how many `turn_end` messages went past, in what order,
whether a background task is still running — is the proxy's problem.

## Python entry points

For tests and for embedding, the same behaviour is reachable without a
subprocess.

| Entry point | Use |
|---|---|
| `agentproxy.cli.serve(stdin, stdout, passthrough)` | run one session over the given streams; `stdin` is binary, `stdout` text |
| `agentproxy.cli.main(argv)` | the console-script entry point |
| `agentproxy.turn_tracker.TurnTracker` | the turn table alone — `submit`, `mark_typed`, `on_entry`, `end_running_turn`, `fail_session`, `outstanding`, `snapshot` |
| `agentproxy.protocol.Emitter` | stamps `seq` and `outstanding` and writes a message |
| `agentproxy.orphans.OrphanReaper` | the adopted-process table: `poll`, `count`, `alive`, `shutdown`; `become_subreaper()` sets the flag |
| `agentproxy.replay.replay(transcript, prompts, out)` | drive a tracker from a recorded transcript, with no `claude` |

`TurnTracker` does no I/O and returns events as plain dicts, which is what
makes a whole session replayable:

```bash
python3 -m agentproxy.replay tests/fixtures/synthetic-session.jsonl --assert-drained
```

## Versioning

The protocol is unversioned and has one consumer at a time; the proxy tracks
whatever `claude` is installed rather than a pinned version. Two things follow
for a client:

- **Ignore unknown message types and unknown fields.** New types are added
  when `claude` grows a shape worth reporting; the envelope does not change.
- **`basis` values are open.** Match on the `marker:` / `fallback:` /
  `cancel:` prefix rather than on the full set.
