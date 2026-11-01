import json
import os
import subprocess
import sys
import time

import pytest

from agentproxy.commands import Rejection, parse_command
from agentproxy.inner_claude import build_argv
from agentproxy.input_bridge import InputBridge
from agentproxy.input_hook import denial
from agentproxy.user_input import InputRequests, input_timeout


@pytest.mark.parametrize('value',['0','-1','nan','inf','bogus','31536001'])
def test_input_deadline_rejects_invalid_configuration(monkeypatch, value):
    monkeypatch.setenv('AGENTPROXY_INPUT_TIMEOUT_S',value)
    with pytest.raises(ValueError, match='AGENTPROXY_INPUT_TIMEOUT_S'): input_timeout()


def test_input_deadline_default_and_override(monkeypatch):
    monkeypatch.delenv('AGENTPROXY_INPUT_TIMEOUT_S',raising=False)
    assert input_timeout() == 86400
    monkeypatch.setenv('AGENTPROXY_INPUT_TIMEOUT_S','42.5')
    assert input_timeout() == 42.5


@pytest.mark.parametrize('command',[{'dismiss':False},{'dismiss':1},{'dismiss':True,'answers':{}}, {'answers':[]}])
def test_answer_parser_rejects_invalid_shape(command):
    result=parse_command(json.dumps({'type':'answer','request':'i-1','ref':'r',**command}))
    assert isinstance(result,Rejection) and result.ref == 'r'


def test_claude_interactive_launch_preserves_explicit_denials():
    assert 'AskUserQuestion' not in build_argv([],user_input=True)
    assert 'AskUserQuestion' in build_argv(['--disallowedTools','AskUserQuestion'],user_input=True)
    assert 'AskUserQuestion' in build_argv([])


def test_bridge_preserves_caller_hook_settings_and_delimiter(tmp_path):
    settings={'env':{'MY_SETTING':'ok'},'hooks':{'PreToolUse':[{'matcher':'Bash','hooks':[]}]}}
    config=tmp_path/'settings.json'; config.write_text(json.dumps(settings))
    bridge=InputBridge()
    try:
        argv=bridge.argv(['--settings',str(config),'--','--bare'],86400)
        merged_path=argv[argv.index('--settings')+1]
        from pathlib import Path
        actual=json.loads(Path(merged_path).read_text())
        assert os.stat(merged_path).st_mode & 0o777 == 0o600
        assert actual['env'] == settings['env']
        assert actual['hooks']['PreToolUse'][0] == settings['hooks']['PreToolUse'][0]
        assert actual['hooks']['PreToolUse'][-1]['hooks'][0]['timeout'] > 86400
        assert argv[-2:] == ['--','--bare']
        assert json.loads(config.read_text()) == settings
        assert os.stat(os.path.dirname(bridge.path)).st_mode & 0o777 == 0o700
    finally: bridge.close()
    assert not os.path.exists(bridge.path)


@pytest.mark.parametrize('args',[['--bare'],['--settings','{"disableAllHooks":true}'],['--settings','missing-file']])
def test_bridge_rejects_unsupported_launch(args):
    bridge=InputBridge()
    try:
        with pytest.raises(ValueError): bridge.argv(args,86400)
    finally: bridge.close()


def test_real_hook_process_waits_and_receives_structured_reply():
    bridge=InputBridge()
    proc=subprocess.Popen([sys.executable,'-m','agentproxy.input_hook',bridge.path],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    payload={'hook_event_name':'PreToolUse','tool_name':'AskUserQuestion','tool_use_id':'native-1','session_id':'session','tool_input':{'questions':[]}}
    proc.stdin.write(json.dumps(payload).encode()); proc.stdin.close()
    seen=[]
    def receive(c,p):
        seen.append(p)
        c.respond(denial('test dismissed'))
    try:
        deadline=time.monotonic()+5
        while proc.poll() is None and time.monotonic()<deadline:
            bridge.poll(receive,lambda c:None)
            time.sleep(.01)
        assert proc.wait(timeout=1) == 0
        assert seen == [payload]
        output=json.loads(proc.stdout.read())
        assert output['hookSpecificOutput']['permissionDecision'] == 'deny'
    finally:
        if proc.poll() is None: proc.kill(); proc.wait()
        bridge.close()


def test_hook_missing_bridge_denies_instead_of_leaving_a_selector(tmp_path):
    payload={'hook_event_name':'PreToolUse','tool_name':'AskUserQuestion'}
    result=subprocess.run([sys.executable,'-m','agentproxy.input_hook',str(tmp_path/'missing')],input=json.dumps(payload),text=True,capture_output=True,timeout=5)
    assert json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_schema_rejects_ambiguous_claude_questions():
    requests=InputRequests()
    q={'question':'Same?','options':[{'label':'Yes'}]}
    with pytest.raises(ValueError,match='identities'):
        requests.add('key',{'id':'t-1','kind':'user'},[q,q],'claude',True,lambda *_:None)
    assert requests.count() == 0


def test_bridge_detects_inherited_disabled_hooks(tmp_path,monkeypatch):
    monkeypatch.setenv('CLAUDE_CONFIG_DIR',str(tmp_path))
    (tmp_path/'settings.json').write_text('{"disableAllHooks":true}')
    bridge=InputBridge()
    try:
        with pytest.raises(ValueError,match='disableAllHooks'):
            bridge.argv([],86400)
        # The CLI can explicitly opt out of user settings for this launch.
        assert bridge.argv(['--setting-sources','project,local'],86400)
    finally: bridge.close()


def test_waiting_hook_disconnect_fails_instead_of_reopening_a_selector():
    import socket
    from tests.harness import Session
    s=Session(user_input=True)
    bridge=InputBridge()
    s.proxy._bridge=bridge
    client=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
    payload={'hook_event_name':'PreToolUse','tool_name':'AskUserQuestion','tool_use_id':'tool-1',
             'session_id':'session-under-test','tool_input':{'questions':[{'question':'Which?','options':[{'label':'A'},{'label':'B'}]}]}}
    try:
        client.connect(bridge.path)
        client.sendall(json.dumps(payload).encode()+b'\n')
        s.tick()
        assert s.emitter.outstanding()['input'] == 1
        client.close()
        with pytest.raises(RuntimeError,match='disconnected'):
            s.tick()
        s.proxy.fail('hook disconnected')
        assert not any(s.emitter.outstanding().values())
        assert len(s.of_type('input_resolved')) == 1
    finally:
        client.close();bridge.close()
