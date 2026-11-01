"""Requirement 17: permission decisions use the existing choice protocol."""
import json

import pytest

from agentproxy.app_server import Notification, ServerRequest
from tests.codex_harness import CodexSessionUnderTest
from tests.harness import Session, user_text
from tests.requirements.test_user_input import Connection, of_type, resolve


def start(agent, timeout=86400):
    if agent == 'claude':
        s = Session(bypass_permissions=False, input_timeout_s=timeout)
        s.submit('permission')
        s.tick()
        s.writes(user_text('permission'))
        s.tick()
    else:
        s = CodexSessionUnderTest(bypass_permissions=False, input_timeout_s=timeout)
        s.submit('run 100')
        s.proxy.on_messages(s.peer.feed(s.server.take_output()))
    return s


def ask(s, agent, request_id=77, **overrides):
    if agent == 'claude':
        c = Connection()
        s.proxy._on_hook(c, {'session_id': 'session-under-test', 'hook_event_name': 'PermissionRequest',
                            'tool_name': 'Bash', 'tool_input': {'command': 'printf harmless'}, **overrides})
        return c
    native_turn = s.proxy._session.codex_turn_of(s.tracker.running_turn.id)
    params = {'threadId': s.proxy._session.thread_id, 'turnId': native_turn,
              'itemId': f'item-{request_id}', 'command': 'printf harmless', **overrides}
    s.proxy.on_messages([ServerRequest(request_id, 'item/commandExecution/requestApproval', params)])


def reply(s, choice='o-1', request='i-1'):
    s.send({'type': 'answer', 'request': request, 'answers': {'q-1': {'choices': [choice]}}, 'ref': 'pick'})


@pytest.mark.parametrize('agent', ['claude', 'codex'])
@pytest.mark.parametrize('allow', [True, False])
def test_requirement_17_permission_uses_choice_ids_and_once_scope(agent, allow):
    s = start(agent)
    ready = s.messages()[0]
    assert ready['permissions'] == {'bypass': False}
    assert ready['capabilities'] == {'user_input': True, 'permission_approvals': True}
    connection = ask(s, agent)
    s.send({'type': 'status'})
    request = s.messages()[-1]['input_requests'][0]
    assert request['kind'] == 'permission' and request['permission']['scope'] == 'operation'
    assert request['turn']['id'] == s.tracker.running_turn.id
    question = request['questions'][0]
    assert not question['allow_text'] and not question['multi_select']
    assert 'printf harmless' in question['question']
    assert [o['label'] for o in question['options']] == ['Allow once', 'Deny']
    reply(s, 'o-1' if allow else 'o-2')
    assert s.messages()[-1]['type'] == 'ack'
    assert s.messages()[-1]['ref'] == 'pick'
    assert s.emitter.outstanding()['input'] == 1
    if agent == 'claude':
        native = connection.outputs[0]['hookSpecificOutput']
        assert native['hookEventName'] == 'PermissionRequest'
        assert native['decision']['behavior'] == ('allow' if allow else 'deny')
        assert 'updatedPermissions' not in native['decision']
        connection.on_delivered()
    else:
        result = s.server.received[-1]['result']
        assert result == {'decision': 'accept' if allow else 'decline'}
        resolve(s, agent)
    assert s.emitter.outstanding()['input'] == 0
    assert of_type(s, 'input_resolved')[-1]['reason'] == 'answered'
    # Neither a previous allow nor a previous deny answers the next operation.
    next_connection = ask(s, agent, request_id=78)
    assert of_type(s, 'input_request')[-1]['request']['id'] == 'i-2'
    assert s.emitter.outstanding()['input'] == 1
    if agent == 'claude':
        assert not next_connection.outputs
    reply(s, request='i-1')
    assert s.messages()[-1]['type'] == 'error'


@pytest.mark.parametrize('agent', ['claude', 'codex'])
@pytest.mark.parametrize('action', ['expire', 'dismiss', 'cancel', 'eof', 'fail'])
def test_requirement_17_closing_never_approves(agent, action):
    s = start(agent, timeout=10)
    c = ask(s, agent)
    if action == 'expire':
        if agent == 'claude':
            s.tick(11, pty_quiet=True)
        else:
            s.now += 11
            s.proxy.on_tick()
    elif action == 'dismiss':
        s.send({'type': 'answer', 'request': 'i-1', 'dismiss': True})
    elif action == 'cancel':
        s.send({'type': 'cancel', 'turn': s.tracker.running_turn.id})
    elif action == 'eof':
        s.proxy.on_stdin_eof()
    else:
        s.proxy.fail('backend died')
    assert s.emitter.outstanding()['input'] == 0
    assert len(of_type(s, 'input_resolved')) == 1
    if action != 'fail':
        if agent == 'claude':
            assert c.outputs[0]['hookSpecificOutput']['decision']['behavior'] == 'deny'
        else:
            assert any(m.get('result') == {'decision': 'decline'} for m in s.server.received)
    else:
        assert not any(m.get('result') == {'decision': 'accept'} for m in s.server.received) if agent == 'codex' else not c.outputs


@pytest.mark.parametrize('agent', ['claude', 'codex'])
@pytest.mark.parametrize('answer', [{'text': 'Allow once'}, {'choices': ['o-1', 'o-2']}, {'choices': ['acceptForSession']}, {'choices': []}])
def test_requirement_17_invalid_answers_do_not_deliver(agent, answer):
    s = start(agent)
    c = ask(s, agent)
    s.send({'type': 'answer', 'request': 'i-1', 'answers': {'q-1': answer}})
    assert s.messages()[-1]['type'] == 'error'
    assert s.emitter.outstanding()['input'] == 1
    if c:
        assert not c.outputs


@pytest.mark.parametrize('agent', ['claude', 'codex'])
def test_requirement_17_permission_wait_suppresses_stall(agent):
    s = start(agent)
    ask(s, agent)
    if agent == 'claude':
        s.tick(7200, pty_quiet=True)
    else:
        s.now += 7200
        s.proxy.on_tick()
    assert s.tracker.running_turn
    assert not of_type(s, 'turn_end')


def test_claude_permission_session_filter_and_unhooked_prompt_failure():
    s = start('claude')
    c = ask(s, 'claude', session_id='foreign')
    assert c.outputs[0]['hookSpecificOutput']['decision']['behavior'] == 'deny'
    assert not of_type(s, 'input_request')
    with pytest.raises(RuntimeError, match='no supported decision hook'):
        s.proxy._on_hook(Connection(), {'session_id': 'session-under-test', 'hook_event_name': 'Notification',
                                       'notification_type': 'permission_prompt'})


def test_codex_honors_available_decisions_without_persistent_grants():
    s = start('codex')
    ask(s, 'codex', availableDecisions=['acceptForSession', 'decline'])
    request = of_type(s, 'input_request')[0]['request']
    assert [o['label'] for o in request['questions'][0]['options']] == ['Deny']
    reply(s)
    assert s.server.received[-1]['result'] == {'decision': 'decline'}


def test_codex_unsupported_broader_permissions_are_refused():
    s = start('codex')
    s.proxy.on_messages([ServerRequest(55, 'item/permissions/requestApproval', {})])
    assert s.server.received[-1]['result'] == {'permissions': {}}
    assert not of_type(s, 'input_request')
    assert 'unsupported permission request' in of_type(s, 'error')[-1]['error']


def test_codex_approval_round_trip_with_fake_backend():
    s = CodexSessionUnderTest(bypass_permissions=False)
    s.submit('approve text')
    s.tick(3)
    assert len(of_type(s, 'input_request')) == 1
    reply(s)
    s.run_until_idle()
    assert of_type(s, 'input_resolved')[0]['reason'] == 'answered'
    assert not any(s.messages()[-1]['outstanding'].values())


def test_codex_native_cancel_is_an_explicit_denial_without_rule_amendment():
    s = start('codex')
    ask(s, 'codex', availableDecisions=['accept', {'acceptWithExecpolicyAmendment': {}}, 'cancel'])
    reply(s, 'o-2')
    assert s.server.received[-1]['result'] == {'decision': 'cancel'}


def test_claude_delayed_notification_during_supported_hook_keeps_waiting():
    s = start('claude')
    c = ask(s, 'claude')
    notification = Connection()
    s.proxy._on_hook(notification, {'session_id': 'session-under-test', 'hook_event_name': 'Notification',
                                     'notification_type': 'permission_prompt'})
    assert notification.outputs == [{}] and not c.outputs
    assert s.emitter.outstanding()['input'] == 1


def test_codex_permission_before_turn_response_waits_for_correct_identity():
    s = CodexSessionUnderTest(bypass_permissions=False)
    s.submit('run 100')
    native_turn = s.server._active
    s.proxy.on_messages([ServerRequest(77, 'item/commandExecution/requestApproval', {
        'threadId': s.proxy._session.thread_id, 'turnId': native_turn, 'itemId': 'cmd', 'command': 'printf ok'})])
    assert not of_type(s, 'input_request')
    s.proxy.on_messages(s.peer.feed(s.server.take_output()))
    assert len(of_type(s, 'input_request')) == 1
    assert of_type(s, 'turn_start')[0]['seq'] < of_type(s, 'input_request')[0]['seq']


def test_codex_foreign_permission_is_not_attributed_to_current_turn():
    s = start('codex')
    ask(s, 'codex', threadId='foreign')
    assert not of_type(s, 'input_request')
    assert 'error' in s.server.received[-1]


def test_codex_file_change_details_appear_in_choices_and_broader_grant_is_refused():
    s = start('codex')
    native_turn = s.proxy._session.codex_turn_of(s.tracker.running_turn.id)
    params = {'threadId': s.proxy._session.thread_id, 'turnId': native_turn, 'itemId': 'patch-1'}
    item = {'id': 'patch-1', 'type': 'fileChange', 'changes': [{'path': '/work/test.txt', 'diff': '+hello'}]}
    s.proxy.on_messages([Notification('item/started', {**params, 'item': item}),
                         ServerRequest(77, 'item/fileChange/requestApproval', params)])
    request = of_type(s, 'input_request')[0]['request']
    assert request['permission']['item']['changes'] == item['changes']
    assert '+hello' in request['questions'][0]['question']
    reply(s)
    assert s.server.received[-1]['result'] == {'decision': 'accept'}
    resolve(s, 'codex')
    s.proxy.on_messages([ServerRequest(78, 'item/fileChange/requestApproval', {**params, 'grantRoot': '/tmp'})])
    assert s.server.received[-1]['result'] == {'decision': 'decline'}
    assert len(of_type(s, 'input_request')) == 1


@pytest.mark.parametrize('agent', ['claude', 'codex'])
def test_permission_answer_delivery_timeout_fails_without_approving_another_operation(agent):
    s = start(agent)
    ask(s, agent)
    reply(s)
    if agent == 'claude':
        with pytest.raises(RuntimeError, match='delivery was not confirmed'):
            s.tick(31)
    else:
        s.now += 31
        s.proxy.on_tick()
        assert not any(s.emitter.outstanding().values())
        assert of_type(s, 'input_resolved')[0]['reason'] == 'error'
