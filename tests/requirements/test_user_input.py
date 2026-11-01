"""Requirement 16 / criterion 10: one client can answer either agent."""
import json

import pytest

from agentproxy.app_server import Notification, ServerRequest
from tests.harness import Session, marker, user_text, tool_result
from tests.codex_harness import CodexSessionUnderTest
from tests.requirements.reference_client import ReferenceClient
from tests.requirements import invariants


def questions(multi=False):
    return [{"id": "color", "header": "Color", "question": "Which color?",
             "options": [{"label": "Blue", "description": "blue"}, {"label": "Green", "description": "green"}],
             "multiSelect": multi, "isOther": True}]


class Connection:
    def __init__(self):
        self.outputs = []
    def respond(self, output):
        self.outputs.append(output)


def start(agent, timeout=86400):
    if agent == "claude":
        s = Session(user_input=True, input_timeout_s=timeout)
        s.submit("ask")
        s.tick()
        s.writes(user_text("ask"))
        s.tick()
    else:
        s = CodexSessionUnderTest(user_input=True, input_timeout_s=timeout)
        s.submit("run 100")
        s.proxy.on_messages(s.peer.feed(s.server.take_output()))
    return s


def ask(s, agent, *, blocking=True, request_id=77, multi=False, raw=None):
    raw = questions(multi) if raw is None else raw
    if agent == "claude":
        c = Connection()
        s.proxy._on_hook(c, {"session_id": "session-under-test", "tool_name": "AskUserQuestion",
                            "tool_use_id": str(request_id), "hook_event_name": "PreToolUse",
                            "tool_input": {"questions": raw}})
        return c
    native_turn = s.proxy._session.codex_turn_of(s.tracker.running_turn.id)
    s.proxy.on_messages([ServerRequest(request_id, "item/tool/requestUserInput", {
        "threadId": s.proxy._session.thread_id, "turnId": native_turn, "itemId": f"item-{request_id}",
        "questions": raw, "isBlocking": blocking})])


def resolve(s, agent, request_id=77):
    if agent == "claude":
        s.writes(tool_result(str(request_id), "answer received"))
        s.tick()
    else:
        s.proxy.on_messages([Notification("serverRequest/resolved", {
            "threadId": s.proxy._session.thread_id, "requestId": request_id})])


def finish(s, agent):
    if agent == "claude":
        s.writes(marker())
        s.tick()
    else:
        s.proxy.fail("test finished")


def of_type(s, kind):
    return [m for m in s.messages() if m['type'] == kind]


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_requirement_16_answer_keeps_turn_identity_and_resync(agent):
    s = start(agent)
    own = s.tracker.running_turn.id
    ask(s, agent)
    s.send({"type": "status", "ref": "recover"})
    state = s.messages()[-1]
    client = ReferenceClient()
    client.resync_to(state)
    client.consume(state)
    assert client.derived == state['outstanding']
    request = next(iter(client.input_requests.values()))
    assert request['turn']['id'] == own
    s.send({"type": "answer", "request": request['id'], "answers": {"q-1": {"choices": ["o-2"]}}, "ref": "pick"})
    assert s.messages()[-1]['type'] == 'ack'
    assert s.messages()[-1]['turn']['id'] == own
    assert s.messages()[-1]['ref'] == 'pick'
    assert s.emitter.outstanding()['input'] == 1  # accepted is not consumed
    resolve(s, agent)
    assert s.emitter.outstanding()['input'] == 0
    s.send({"type": "answer", "request": request['id'], "answers": {"q-1": {"choices": ["o-2"]}}, "ref": "late"})
    assert s.messages()[-1]['type'] == 'error' and s.messages()[-1]['ref'] == 'late'
    finish(s, agent)
    full = ReferenceClient()
    for message in s.messages():
        full.consume(message)
    assert full.derived == s.messages()[-1]['outstanding']
    invariants.all_of_them(s.messages())


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_criterion_10_human_wait_does_not_trigger_turn_timeouts(agent):
    s = start(agent)
    own = s.tracker.running_turn.id
    ask(s, agent)
    if agent == 'claude':
        s.tick(3600, pty_quiet=True)
    else:
        s.now += 3600
        s.proxy.on_tick()
    assert s.tracker.running_turn.id == own
    assert not of_type(s, 'turn_end')
    s.send({"type": "answer", "request": "i-1", "answers": {"q-1": {"text": "Teal"}}})
    if agent == 'claude':
        s.tick()
    else:
        s.proxy.on_tick()
    assert s.tracker.running_turn.id == own
    resolve(s, agent)
    finish(s, agent)


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("action,reason", [('expire','expired'), ('dismiss','dismissed'), ('cancel','cancelled'), ('eof','dismissed'), ('fail','error')])
def test_requirement_16_every_closing_drains_questions(agent, action, reason):
    s = start(agent, timeout=10)
    ask(s, agent)
    if action == 'expire':
        if agent == 'claude': s.tick(11)
        else: s.now += 11; s.proxy.on_tick()
    elif action == 'dismiss': s.send({'type':'answer','request':'i-1','dismiss':True})
    elif action == 'cancel': s.send({'type':'cancel','turn':s.tracker.running_turn.id})
    elif action == 'eof': s.proxy.on_stdin_eof()
    else: s.proxy.fail('failed')
    assert s.emitter.outstanding()['input'] == 0
    assert of_type(s,'input_resolved')[-1]['reason'] == reason
    resolve(s, agent)
    assert len(of_type(s,'input_resolved')) == 1


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize('answers', [{}, {'q-1': {'choices': ['bad']}}, {'q-1': {'choices':['o-1','o-2']}},
                                     {'q-1': {'choices':['o-1'], 'text':'x'}}, {'q-1':{'text':''}}])
def test_requirement_16_invalid_batch_is_atomic(agent, answers):
    s = start(agent)
    connection = ask(s, agent)
    before = len(s.server.answers) if agent == 'codex' else len(connection.outputs)
    s.send({'type':'answer','request':'i-1','answers':answers,'ref':'invalid'})
    assert s.messages()[-1]['type'] == 'error'
    assert s.messages()[-1]['ref'] == 'invalid'
    assert s.proxy._inputs.snapshot()[0]['state'] == 'pending'
    assert before == (len(s.server.answers) if agent == 'codex' else len(connection.outputs))


def test_requirement_16_claude_batch_multiselect_and_free_text():
    s = start('claude')
    raw = questions(True) + [{**questions()[0], 'question':'Which shade?'}]
    c = ask(s, 'claude', raw=raw)
    s.send({'type':'answer','request':'i-1','answers':{'q-1':{'choices':['o-1','o-2']},'q-2':{'text':'Pale'}}})
    assert c.outputs[-1]['hookSpecificOutput']['updatedInput']['answers'] == {'Which color?':'Blue, Green','Which shade?':'Pale'}


def test_requirement_16_nonblocking_codex_question_outlives_turn():
    s = start('codex')
    own = s.tracker.running_turn.id
    native = s.proxy._session.codex_turn_of(own)
    ask(s, 'codex', blocking=False)
    s.proxy.on_messages([Notification('turn/completed', {'threadId':s.proxy._session.thread_id,'turn':{'id':native,'status':'completed'}})])
    assert s.tracker.running_turn is None
    assert s.emitter.outstanding()['input'] == 1
    s.send({'type':'answer','request':'i-1','answers':{'q-1':{'text':'Teal'}}})
    resolve(s,'codex')
    assert len(of_type(s,'turn_start')) == 1
    assert len(of_type(s,'turn_end')) == 1
    assert s.messages()[-1]['turn']['id'] == own
    client = ReferenceClient()
    for m in s.messages(): client.consume(m)
    assert client.derived == s.messages()[-1]['outstanding']


@pytest.mark.parametrize('agent',['claude','codex'])
def test_requirement_16_delivery_watchdog_cannot_wait_forever(agent):
    s = start(agent)
    ask(s, agent)
    s.send({'type':'answer','request':'i-1','answers':{'q-1':{'text':'Teal'}}})
    if agent == 'claude':
        with pytest.raises(RuntimeError, match='delivery'):
            s.tick(31)
    else:
        s.now += 31; s.proxy.on_tick()
        assert s.proxy.failed
        assert s.emitter.outstanding()['input'] == 0


@pytest.mark.parametrize('agent',['claude','codex'])
def test_requirement_16_answer_cancel_race_resolves_once(agent):
    s=start(agent)
    ask(s,agent)
    s.send({'type':'answer','request':'i-1','answers':{'q-1':{'choices':['o-2']}}})
    s.send({'type':'cancel','turn':s.tracker.running_turn.id})
    resolve(s,agent)
    assert [m['reason'] for m in of_type(s,'input_resolved')] == ['cancelled']
    assert s.emitter.outstanding()['input'] == 0


def test_requirement_16_claude_prompt_queues_behind_question():
    s=start('claude')
    ask(s,'claude')
    queued=s.submit('next')
    s.tick(60)
    assert s.typed == ['ask']
    s.send({'type':'answer','request':'i-1','answers':{'q-1':{'choices':['o-2']}}})
    resolve(s,'claude');finish(s,'claude')
    s.tick()
    assert s.typed == ['ask','next']
    assert s.tracker.typed_turn.id == queued


@pytest.mark.parametrize('agent',['claude','codex'])
def test_requirement_16_new_question_after_eof_is_dismissed(agent):
    s=start(agent)
    s.proxy.on_stdin_eof()
    ask(s,agent)
    assert s.emitter.outstanding()['input'] == 0
    assert of_type(s,'input_resolved')[-1]['reason'] == 'dismissed'


def test_requirement_16_claude_hook_confirms_unowned_turn():
    s=Session(user_input=True)
    ask(s,'claude')
    assert s.tracker.running_turn.kind == 'unowned'
    assert of_type(s,'input_request')[0]['turn'] == of_type(s,'turn_start')[0]['turn']
    s.send({'type':'answer','request':'i-1','dismiss':True})
    assert s.emitter.outstanding()['input'] == 0


def test_requirement_16_claude_foreign_hook_is_not_misattributed():
    s=start('claude')
    c=Connection()
    s.proxy._on_hook(c,{'session_id':'foreign','tool_name':'AskUserQuestion','tool_use_id':'x','hook_event_name':'PreToolUse','tool_input':{'questions':questions()}})
    assert not of_type(s,'input_request')
    assert c.outputs[-1]['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_requirement_16_codex_request_before_turn_response_is_held():
    s=CodexSessionUnderTest(user_input=True)
    s.submit('run 100')
    native=s.server._active
    s.proxy.on_messages([ServerRequest(77,'item/tool/requestUserInput',{
        'threadId':s.proxy._session.thread_id,'turnId':native,'itemId':'item', 'questions':questions()})])
    assert not of_type(s,'input_request')
    s.proxy.on_messages(s.peer.feed(s.server.take_output()))
    assert len(of_type(s,'input_request')) == 1
    assert of_type(s,'turn_start')[0]['seq'] < of_type(s,'input_request')[0]['seq']


def test_requirement_16_codex_native_closure_rejects_stale_reply():
    s=start('codex')
    ask(s,'codex')
    resolve(s,'codex')
    assert of_type(s,'input_resolved')[-1]['reason'] == 'backend_closed'
    before=len(s.server.answers)
    s.send({'type':'answer','request':'i-1','dismiss':True})
    assert s.messages()[-1]['type'] == 'error'
    assert len(s.server.answers) == before


@pytest.mark.parametrize('agent',['claude','codex'])
def test_requirement_16_two_questions_in_one_turn_get_distinct_ids(agent):
    s=start(agent)
    ask(s,agent)
    s.send({'type':'answer','request':'i-1','dismiss':True})
    ask(s,agent,request_id=78)
    assert [m['request']['id'] for m in of_type(s,'input_request')] == ['i-1','i-2']
    assert len({m['turn']['id'] for m in of_type(s,'input_request')}) == 1
