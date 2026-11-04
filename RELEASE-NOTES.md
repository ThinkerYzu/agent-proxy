# agent-proxy v0.3.0 (withdrawn)

Withdrawn at the maintainer's request. The GitHub Release, its assets and the
v0.3.0 tag have been removed. v0.2.0 is the latest published release.
The notes below describe the retained implementation in this checkout.

This release adds structured user questions and client permission approvals
for both Claude and Codex. Existing invocations keep their current behavior.

## Structured user input

- `--user-input` relays Claude `AskUserQuestion` and Codex
  `item/tool/requestUserInput` through the same client protocol. Requests
  carry question/option IDs, labels, descriptions, selection rules, text
  support, secret markers and whether the request blocks its turn.
- `input_request`, the `answer` command and `input_resolved` identify the
  interaction within its existing turn. Answers are validated atomically;
  invalid, duplicate and stale replies have no backend side effect.
- `ready.capabilities.user_input`, `outstanding.input` and
  `state.input_requests` let clients discover the capability and recover
  authoritative pending state. Nonblocking Codex questions may outlive their
  originating turn.
- The deadline defaults to 24 hours. Configure seconds with
  `AGENTPROXY_INPUT_TIMEOUT_S` (positive, finite, at most one year).
  Blocking human waits suspend turn fallbacks; answer delivery has its own
  30-second watchdog. Expiry, dismissal, EOF, cancellation and backend
  failure resolve requests without selecting an answer automatically.

## Optional permission checks

- `--no-bypass-permissions` enables native checks and implies `--user-input`.
  Supported requests show operation details and **Allow once / Deny** choices
  through the existing choice-prompt flow. No persistent rule, policy
  amendment or session-wide grant is inferred.
- Claude uses ordinary `manual` mode unless an explicit non-bypass mode is
  supplied. Codex uses `workspace-write`, on-request approvals and the user
  reviewer, preserving an explicit `read-only` sandbox. These settings apply
  on new/resumed/forked sessions without changing saved configuration.
- Explicit contradictory bypass arguments fail startup. Restricted launches
  announce `ready.permissions.bypass: false` and
  `ready.capabilities.permission_approvals: true`.
- Dismissal, expiry, cancellation and EOF deny pending requests. Approval
  transport failure fails the session. A subsequent operation needs a new
  decision. When Codex offers `cancel` as its native denial, Deny interrupts
  the native turn.

## Client compatibility and backend requirements

This is a minor release with additive commands, events and fields. Default
launches retain permission bypass and keep user input disabled. Clients must
support the choice-request flow before enabling either new flag. Independent
disabling of ordinary questions while keeping permission choices is not
supported. Request IDs are process-local; discard them when restarting.

Claude Code **2.1.221+** remains sufficient for ordinary launches. Input and
permission features require **2.1.316+**, working hooks and a private Unix
socket; `--bare` and disabled hooks are rejected. Main-session tool hooks are
supported. Claude sandbox-network prompts lack a PermissionRequest decision
hook; unmatched permission notifications fail explicitly.

Codex requires ChatGPT authentication; API-key billing remains disabled.
The app-server protocol is experimental. Supported approvals are command/file
operation decisions. Broader permission grants, legacy variants and file
requests with a session `grantRoot` are explicitly refused. General MCP forms
retain their existing policies. See the [checkout API](https://github.com/ThinkerYzu/agent-proxy/blob/master/API.md)
for the full contract.

## Installation

Install the latest published release (without the input features above):

```bash
pip install 'git+https://github.com/ThinkerYzu/agent-proxy@v0.2.0'
```

The v0.3.0 wheel and source distribution have been withdrawn.
No PyPI publication is planned.

## Validation

Validated locally on 2026-11-03:

- All 514 offline tests passed on Python 3.14.4 in the checkout (4.78 s).
- All 20 live tests passed without skips on Claude Code 2.1.317 and Codex
  CLI 0.166.0 (8 min 15 s), including structured questions, permission
  approval/denial/expiry, cancellation and session recovery.
- A clean Python 3.11.16 environment installed the built wheel and passed
  all 514 offline tests (4.90 s). Package/module versions agree at 0.3.0;
  the console command and packaged input-hook modules are present.
- Both checkout and installed-wheel replay drained all counters. Wheel and
  source distribution builds succeeded.

Hosted CI runs the offline suite and drained replay on Python 3.11–3.14.
The release workflow verifies the tag/version match, tests, builds both
artifacts and checks the installed wheel before publishing.
