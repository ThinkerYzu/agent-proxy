# agent-proxy v0.3.1

This is the first PyPI release under **`thinkeryzu-agent-proxy`**, with the
unchanged **`agent-proxy`** command and **`agentproxy`** Python import. It adds
structured user questions and client permission approvals since v0.2.0. Default
invocations retain their current behavior. v0.3.0 was withdrawn; v0.3.1 uses a
new version rather than recreating that tag.

## Installation

```bash
uv tool install thinkeryzu-agent-proxy==0.3.1
agent-proxy --help
```

Or use `uvx --from thinkeryzu-agent-proxy==0.3.1 agent-proxy --help`,
`uv pip install thinkeryzu-agent-proxy==0.3.1` or
`pip install thinkeryzu-agent-proxy==0.3.1`. GitHub provides the same validated
wheel and source distribution, plus an installable v0.3.1 tag.

Older GitHub releases used distribution metadata named `agent-proxy`. Update
dependency references to `thinkeryzu-agent-proxy`; CLI/import names are unchanged.
Claude/Codex binaries must be installed and authenticated separately.

## Structured user input

- `--user-input` relays Claude `AskUserQuestion` and Codex
  `item/tool/requestUserInput` through the same client protocol. Requests
  carry stable question/option IDs, labels, descriptions, selection rules,
  text support, secret markers and whether the request blocks its turn.
- `input_request`, `answer` and `input_resolved` identify the interaction
  within its existing turn. Answers are validated atomically; invalid,
  duplicate and stale replies have no backend side effect.
- `ready.capabilities.user_input`, `outstanding.input` and
  `state.input_requests` support capability discovery and state recovery.
  Nonblocking Codex questions may outlive their originating turn.
- The request deadline defaults to 24 hours. Configure seconds with
  `AGENTPROXY_INPUT_TIMEOUT_S` (positive, finite, at most one year).
  Blocking human waits suspend turn fallbacks; delivery has a separate
  30-second watchdog. Expiry, dismissal, EOF, cancellation and failure resolve
  pending requests without choosing an answer automatically.

## Optional permission checks

- `--no-bypass-permissions` enables native checks and implies `--user-input`.
  Supported asks show operation details and **Allow once / Deny** choices.
  No persistent rule, policy amendment or session-wide grant is inferred.
- Claude uses ordinary `manual` mode unless another non-bypass mode is
  explicit. Codex uses `workspace-write`, on-request approvals and the user
  reviewer, preserving explicit `read-only`. Apply the flag again on resume
  or fork; no saved settings change.
- Conflicting explicit bypass arguments fail startup. Restricted launches
  announce `ready.permissions.bypass: false` and
  `ready.capabilities.permission_approvals: true`.
- Dismissal, expiry, cancellation and EOF deny pending requests. Broken
  approval transport fails the session. Each operation needs its own decision.
  When Codex offers only `cancel` as denial, Deny interrupts the native turn.

## Compatibility and backend requirements

This release adds protocol commands, events and fields. Enable either input
flag only with a client that renders and answers choice prompts. Default
launches retain bypass and keep user input disabled. Independently disabling
ordinary questions while retaining permission choices is not supported.
Request IDs are process-local; discard them when restarting the proxy.

Claude Code **2.1.221+** remains sufficient for ordinary launches. Input and
permission features require **2.1.316+**, working hooks and a private Unix socket.
`--bare` and disabled hooks are rejected. Main-session tool hooks are supported;
unmatched sandbox-network permission prompts fail explicitly because they have
no PermissionRequest decision hook.

Codex requires ChatGPT authentication; API-key billing stays disabled. Its
app-server protocol is experimental. Command/file operation approvals are
supported. Broader permission grants, legacy variants and file requests with a
session `grantRoot` are explicitly refused. General MCP forms retain their
existing policies. See the [v0.3.1 API](https://github.com/ThinkerYzu/agent-proxy/blob/v0.3.1/API.md).

## Publication

The tag workflow builds once and validates the wheel/source distribution,
publishes through PyPI OIDC Trusted Publishing, then attaches those exact
artifacts to GitHub. No stored PyPI API token is required. A GitHub release/tag
delete does not undo a PyPI upload; PyPI yanking is a separate account action.

## Validation

Validated locally on 2026-11-03:

- All 533 offline tests passed on Python 3.14.4 (4.86 s), and from a clean
  Python 3.11.16 wheel installation (4.76 s).
- All 20 live tests passed without skips on Claude Code 2.1.318 and Codex CLI
  0.166.0 in 470.83 s, including approval/denial/expiry, questions,
  cancellation and session recovery.
- actionlint, strict Twine checks, matching tag/package/module/artifact
  versions, uv tool installation and drained replay passed.

Hosted CI checks Python 3.11–3.14. The tag workflow also tests the installed
wheel/source distributions and uv entry point before uploading to PyPI.
