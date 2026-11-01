"""Native permission asks must reach the client before an operation executes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tests.live.test_live_session import Reader

pytestmark = pytest.mark.live


@pytest.mark.parametrize('agent', ['claude', 'codex'])
@pytest.mark.parametrize('decision', ['allow', 'deny', 'expire'])
def test_live_permission_decision(agent, decision, tmp_path):
    if os.environ.get('AGENTPROXY_RUN_LIVE') != '1':
        pytest.skip('set AGENTPROXY_RUN_LIVE=1')
    work = tmp_path / 'work'
    work.mkdir()
    marker = tmp_path / 'approved-marker'
    argv = [sys.executable, '-m', 'agentproxy', '--agent', agent, '--no-bypass-permissions']
    if agent == 'claude':
        argv += ['--settings', json.dumps({'permissions': {'ask': ['Bash']}})]
        prompt = (f'Use Bash exactly once to run: printf approved > {marker} . '
                  'Do not use other tools or retry if denied. After the result, reply DONE.')
    else:
        prompt = (f'Execute exactly this shell command with sandbox_permissions="require_escalated" '
                  f'and justification="Write the permission test marker outside the workspace": '
                  f'printf approved > {marker} . This is a test of client approval. '
                  'Do not try without escalation or use other tools or retry if denied. Then reply DONE.')
    env = {**os.environ, 'AGENTPROXY_INPUT_TIMEOUT_S': '2' if decision == 'expire' else '120'}
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=work, env=env)
    reader = Reader(proc)
    def send(command):
        proc.stdin.write(json.dumps(command) + '\n')
        proc.stdin.flush()
    try:
        send({'type': 'submit', 'text': prompt})
        messages = reader.wait_for(lambda ms: any(m['type'] in ('input_request', 'turn_end', 'error') for m in ms), 90)
        assert any(m['type'] == 'input_request' for m in messages), json.dumps(messages, indent=2)
        asked = next(m for m in messages if m['type'] == 'input_request')
        request = asked['request']
        assert request['kind'] == 'permission', messages
        assert request['permission']['scope'] == 'operation'
        assert not marker.exists(), messages
        if decision != 'expire':
            # Give the native agent time to prove it is waiting for the client.
            time.sleep(7 if agent == 'claude' and decision == 'allow' else 1)
            assert not marker.exists()
            send({'type': 'answer', 'request': request['id'], 'answers': {
                'q-1': {'choices': ['o-1' if decision == 'allow' else 'o-2']}}})
        messages = reader.wait_for(lambda ms: any(m['type'] == 'turn_end' for m in ms), 90)
        assert not [m for m in messages if m['type'] == 'error'], messages
        resolved = [m for m in messages if m['type'] == 'input_resolved']
        assert len(resolved) == 1, messages
        assert resolved[0]['reason'] == ('expired' if decision == 'expire' else 'answered'), messages
        assert marker.exists() == (decision == 'allow'), messages
        if marker.exists():
            assert marker.read_text() == 'approved'
        assert messages[-1]['outstanding']['input'] == 0
        proc.stdin.close()
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
