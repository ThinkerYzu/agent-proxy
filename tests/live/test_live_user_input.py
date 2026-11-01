"""Native choice round trips, opt in like the existing live tiers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tests.live.test_live_session import Reader

pytestmark = pytest.mark.live
REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('agent',['claude','codex'])
def test_live_user_input_choices_and_text(agent, tmp_path):
    if os.environ.get('AGENTPROXY_RUN_LIVE') != '1':
        pytest.skip('set AGENTPROXY_RUN_LIVE=1')
    if agent == 'claude':
        prompt=('Call AskUserQuestion exactly once with three questions in a single call. '
                'Question 1 header Color, question Which color?, options Blue (blue) and Green (green), multiSelect false. '
                'Question 2 header Extras, question Which extras?, options Hat (hat) and Scarf (scarf), multiSelect true. '
                'Question 3 header Name, question Which name?, options Ada (Ada) and Lin (Lin), multiSelect false. '
                'Wait for my answers. Then reply exactly INPUT_RECEIVED. Do not ask again or choose for me.')
    else:
        prompt=('Use request_user_input to ask two questions in one call: id color, header Color, question Which color?, '
                'options Blue (blue) and Green (green); id name, header Name, question Which name?, '
                'options Ada (Ada) and Lin (Lin). Wait for the answers, then reply exactly INPUT_RECEIVED. '
                'Do not choose for me or use any other tools.')
    env=dict(os.environ)
    env['AGENTPROXY_INPUT_TIMEOUT_S']='120'
    proc=subprocess.Popen([sys.executable,'-m','agentproxy.cli','--agent',agent,'--user-input'],
                          stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          text=True,cwd=tmp_path,env=env)
    reader=Reader(proc)
    def send(command):
        proc.stdin.write(json.dumps(command)+'\n');proc.stdin.flush()
    try:
        send({'type':'submit','text':prompt,'ref':'question-turn'})
        messages=reader.wait_for(lambda ms:any(m['type']=='input_request' for m in ms),90)
        requests=[m for m in messages if m['type']=='input_request']
        assert requests, messages
        request=requests[0]['request']
        assert request['questions']
        answers={}
        for q in request['questions']:
            if q['question']=='Which color?':
                option=next(o for o in q['options'] if o['label']=='Green')
                answers[q['id']]={'choices':[option['id']]}
            elif q['multi_select']:
                answers[q['id']]={'choices':[o['id'] for o in q['options']]}
            else:
                assert q['allow_text']
                answers[q['id']]={'text':'Custom Name'}
        send({'type':'answer','request':request['id'],'answers':answers,'ref':'answer'})
        messages=reader.wait_for(lambda ms:any(m['type']=='input_resolved' for m in ms)
                                and any(m['type']=='turn_end' for m in ms),90)
        resolutions=[m for m in messages if m['type']=='input_resolved']
        assert len(resolutions)==1 and resolutions[0]['reason']=='answered', messages
        assert any(m['type']=='ack' and m.get('ref')=='answer' for m in messages)
        assert any(m['type']=='turn_end' and m['outcome']=='success' for m in messages),messages
        assert any(m['type']=='text' and 'INPUT_RECEIVED' in m.get('text','') for m in messages),messages
        assert messages[-1]['outstanding']['input']==0
        # The next prompt must bind to another user turn, with no leftover question.
        send({'type':'submit','text':'Reply exactly AFTER_INPUT','ref':'next'})
        messages=reader.wait_for(lambda ms:len([m for m in ms if m['type']=='turn_end'])==2,60)
        ends=[m for m in messages if m['type']=='turn_end']
        assert len(ends)==2 and ends[0]['turn']['id']!=ends[1]['turn']['id'],messages
        assert all(e['outcome']=='success' for e in ends)
        proc.stdin.close()
        assert proc.wait(timeout=20)==0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()


@pytest.mark.parametrize('closing',['expire','cancel'])
def test_live_claude_question_closing(closing,tmp_path):
    if os.environ.get('AGENTPROXY_RUN_LIVE') != '1':
        pytest.skip('set AGENTPROXY_RUN_LIVE=1')
    env={**os.environ,'AGENTPROXY_INPUT_TIMEOUT_S':'2' if closing=='expire' else '120'}
    proc=subprocess.Popen([sys.executable,'-m','agentproxy','--user-input'],stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=tmp_path,env=env)
    reader=Reader(proc)
    def send(c):proc.stdin.write(json.dumps(c)+'\n');proc.stdin.flush()
    try:
        send({'type':'submit','text':'Use AskUserQuestion exactly once. Ask Which color? with options Blue (blue) and Green (green). Wait for the answer. If it is denied, reply exactly NO_ANSWER and do not ask again.','ref':'ask'})
        messages=reader.wait_for(lambda ms:any(m['type']=='input_request' for m in ms),90)
        asked=next(m for m in messages if m['type']=='input_request')
        if closing=='cancel':send({'type':'cancel','turn':asked['turn']['id'],'ref':'cancel'})
        messages=reader.wait_for(lambda ms:any(m['type']=='input_resolved' for m in ms) and any(m['type']=='turn_end' for m in ms),60)
        resolutions=[m for m in messages if m['type']=='input_resolved']
        ends=[m for m in messages if m['type']=='turn_end']
        assert len(resolutions)==1 and resolutions[0]['reason']==('expired' if closing=='expire' else 'cancelled'),messages
        assert len(ends)==1,messages
        if closing=='cancel':assert ends[0]['outcome']=='cancelled',messages
        assert messages[-1]['outstanding']['input']==0
        send({'type':'submit','text':'Reply exactly AFTER_CLOSING','ref':'next'})
        messages=reader.wait_for(lambda ms:len([m for m in ms if m['type']=='turn_end'])==2,60)
        assert len([m for m in messages if m['type']=='turn_end'])==2,messages
        proc.stdin.close()
        assert proc.wait(timeout=20)==0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
