"""Launch policy and private native permission transport."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from agentproxy import cli
from agentproxy.inner_claude import build_argv
from agentproxy.inner_codex import plan, with_latest
from agentproxy.input_bridge import InputBridge
from agentproxy.input_hook import denial


@pytest.mark.parametrize('args', [[], ['--resume', 'saved'], ['--continue'], ['--resume', 'saved', '--fork-session']])
def test_codex_restricted_policy_applies_on_every_session_path(args):
    p = plan(args, '/work', bypass_permissions=False)
    expected = {'approvalPolicy': 'on-request', 'approvalsReviewer': 'user', 'sandbox': 'workspace-write'}
    assert expected.items() <= p.open_params.items()
    if p.latest:
        for response in ({'data': []}, {'data': [{'id': 'saved'}]}):
            assert expected.items() <= with_latest(p, response).open_params.items()
    assert plan(['--sandbox', 'read-only'], '/work', bypass_permissions=False).open_params['sandbox'] == 'read-only'


@pytest.mark.parametrize('args', [['--dangerously-skip-permissions'], ['--sandbox', 'danger-full-access'], ['--sandbox=danger-full-access']])
def test_codex_rejects_explicit_bypass(args):
    with pytest.raises(ValueError, match='conflicts'):
        plan(args, '/work', bypass_permissions=False)


@pytest.mark.parametrize('args', [['--dangerously-skip-permissions'], ['--allow-dangerously-skip-permissions'],
                                   ['--permission-mode=bypassPermissions'], ['--permission-mode', 'bypassPermissions']])
def test_claude_rejects_explicit_bypass_before_delimiter(args):
    with pytest.raises(ValueError, match='conflicts'):
        build_argv(args, bypass_permissions=False)
    argv = build_argv(['--', *args], bypass_permissions=False)
    assert argv[-len(args)-1:] == ['--', *args]
    assert argv[:5] == ['claude', '--permission-mode', 'manual', '--autocompact', '1m']


def test_claude_overrides_saved_mode_but_preserves_explicit_rules():
    argv = build_argv(['--settings', '{"permissions":{"defaultMode":"bypassPermissions"}}'], bypass_permissions=False)
    assert argv[1:3] == ['--permission-mode', 'manual']
    assert '--dangerously-skip-permissions' not in argv
    argv = build_argv(['--permission-mode=plan', '--allowedTools', 'Read', '--disallowedTools', 'Bash'], bypass_permissions=False)
    assert '--permission-mode=plan' in argv and '--permission-mode' not in argv
    assert 'Read' in argv and 'Bash' in argv


@pytest.mark.parametrize('agent', ['claude', 'codex'])
def test_cli_restricted_flag_is_consumed_and_passed_to_backend(monkeypatch, agent):
    calls = []
    def serve(*args, **kwargs):
        calls.append(kwargs)
        return 0
    monkeypatch.setattr(cli, 'serve' if agent == 'claude' else 'serve_codex', serve)
    assert cli.main(['--agent', agent, '--no-bypass-permissions']) == 0
    assert calls[0]['bypass_permissions'] is False
    assert '--no-bypass-permissions' not in calls[0]['passthrough']


def test_claude_launch_conflict_is_typed_error_before_spawn(monkeypatch):
    def no_spawn(*args, **kwargs):
        pytest.fail('must reject before spawning')
    monkeypatch.setattr(cli, 'PtyDriver', no_spawn)
    out = io.StringIO()
    assert cli.serve(io.BytesIO(), out, ['--dangerously-skip-permissions'], bypass_permissions=False) == 2
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(events) == 1 and events[0]['type'] == 'error'


def test_permission_bridge_preserves_saved_settings_and_adds_only_launch_hooks():
    bridge = InputBridge()
    settings = {'permissions': {'deny': ['Bash(rm *)']}, 'hooks': {'PermissionRequest': [{'matcher': 'Read', 'hooks': []}]}}
    try:
        argv = bridge.argv(['--settings', json.dumps(settings)], 86400, permission_approvals=True)
        merged = json.loads(Path(argv[argv.index('--settings') + 1]).read_text())
        assert merged['permissions'] == settings['permissions']
        assert merged['hooks']['PermissionRequest'][0] == settings['hooks']['PermissionRequest'][0]
        assert merged['hooks']['PermissionRequest'][-1]['matcher'] == '.*'
        assert merged['hooks']['Notification'][-1]['matcher'] == 'permission_prompt'
    finally:
        bridge.close()


@pytest.mark.parametrize('allow', [True, False])
def test_permission_hook_flushes_native_decision_then_confirms_delivery(allow):
    bridge = InputBridge()
    payload = {'session_id': 'main', 'hook_event_name': 'PermissionRequest', 'tool_name': 'Bash', 'tool_input': {'command': 'printf ok'}}
    proc = subprocess.Popen([sys.executable, '-m', 'agentproxy.input_hook', bridge.path], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.stdin.write(json.dumps(payload).encode())
    proc.stdin.close()
    received, confirmed, disconnected = [], [], []
    output = {'hookSpecificOutput': {'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'allow'}}} if allow else denial('no', 'PermissionRequest')
    def receive(c, p):
        received.append(p)
        c.on_delivered = lambda: confirmed.append(True)
        c.respond(output)
    try:
        deadline = time.monotonic() + 5
        while not confirmed and time.monotonic() < deadline:
            bridge.poll(receive, lambda c: disconnected.append(c))
            time.sleep(.01)
        assert proc.wait(timeout=1) == 0
        assert received == [payload] and confirmed == [True] and not disconnected
        assert json.loads(proc.stdout.read()) == output
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        bridge.close()


def test_permission_hook_bridge_failure_returns_native_denial(tmp_path):
    proc = subprocess.run([sys.executable, '-m', 'agentproxy.input_hook', str(tmp_path / 'missing')],
                          input=json.dumps({'hook_event_name': 'PermissionRequest', 'tool_name': 'Bash'}),
                          capture_output=True, text=True, timeout=5)
    assert json.loads(proc.stdout)['hookSpecificOutput']['decision']['behavior'] == 'deny'
