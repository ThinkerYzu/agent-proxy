# agent-proxy v0.2.0

This release adds Codex as a second backend, turn cancellation, and Linux
orphan-process tracking. It also fixes turn binding and background-work
accounting failures seen since v0.1.0.

## Compatibility and installation

This is a minor release because it adds commands, message types and fields.
Clients must ignore unknown message types and fields, accept new `basis`
values, and inspect all outstanding counts. See [API.md](https://github.com/ThinkerYzu/agent-proxy/blob/v0.2.0/API.md) for the
complete contract.

- Claude Code **2.1.221 or newer** is required by the default
  `--autocompact 1m` launch option. Explicit caller overrides before `--`
  are preserved. This avoids the optional window-size dialog blocking prompts.
- Codex requires an installed `codex` binary signed in with ChatGPT. The
  app-server protocol is experimental. The proxy removes API-key environment
  variables and refuses non-ChatGPT authentication.
- The Codex backend defaults to full access with no approval prompts;
  callers can select a narrower `--sandbox` mode.
- Session ids belong to one backend: store the agent alongside the id so
  resumes select the same backend.
- Linux adds `outstanding.orphan`; it counts processes, so do not add it to
  task counts. The key is absent where adoption is unavailable.

Install from the release tag:

```bash
pip install 'git+https://github.com/ThinkerYzu/agent-proxy@v0.2.0'
```

The GitHub Release also provides a wheel and source distribution. There is
no PyPI release.

## New capabilities

- `--agent codex` drives `codex app-server` behind the same stdio protocol:
  queued prompts, session resume/fork, cancellation, tools and turn endings.
  `ready.agent`, the `codex:` basis family, and `turn_end.error` describe
  backend-specific state. Detached commands are observed as Linux orphans;
  commands still running after cancellation count as background tasks until
  Codex reports their end.
- `cancel` withdraws a queued turn or requests interruption of a running
  user turn. The ending is observed before reporting `outcome: "cancelled"`.
  Claude's restored composer is cleared before the next prompt. New ending
  evidence includes `marker:interrupted`, `cancel:restored` and
  `cancel:withdrawn`.
- Linux child-subreaper support adopts escaped processes and reports
  `orphan_start`, `orphan_end`, `outstanding.orphan`, and `state.orphans`.
  Shutdown leaves them running by default and reports them as abandoned;
  `AGENTPROXY_KILL_ORPHANS=1` requests termination instead.
- Background shell commands, monitors and subagents have distinct
  `background`, `monitor` and `agent` counts and `task.kind` values.
  Task messages include the shell command and a bounded output preview.

## Fixes

- Bind prompts inside Claude's `pasted_content` wrapper, introduced in
  2.1.277. v0.1.0 fails to bind those prompts.
- End typed prompts that never match the transcript with
  `fallback:unmatched`, rather than leave their turns outstanding forever.
- Count background work across completed, failed, killed and stopped
  endings, including a successful `TaskStop`.
- Reject phantom task launches quoted in tool output and incomplete task
  notifications. Match real launch wording at the start of tool results
  without a length cap that would discard long valid confirmations.
- Answer startup dialogs by reading the selected option rather than
  sending a fixed key sequence.

## Validation

Validated locally on 2026-10-20:

- 396 offline tests passed on Python 3.14.4 in the checkout.
- All 10 live tests passed: 7 against Claude Code 2.1.304 and 3 against
  Codex CLI 0.163.0 (6 min 23 s combined).
- A clean Python 3.11.16 environment installed the built wheel and passed
  all 396 offline tests. Package and module versions agree at 0.2.0, and
  the console entry point is present.
- The synthetic replay drained all counters from both the checkout and
  the installed wheel. Wheel and source distribution builds succeeded.

Hosted CI and its full Python 3.11–3.14 matrix will run when pushed.
