"""The stdin command parser every backend answers through."""

from __future__ import annotations

import pytest

from agentproxy.commands import CANCEL, STATUS, SUBMIT, Command, Rejection, parse_command, tagged


def test_a_submit_carries_its_text_and_ref():
    assert parse_command('{"type": "submit", "text": "hi", "ref": "c-1"}') == Command(
        SUBMIT, "c-1", text="hi"
    )


def test_a_status_needs_nothing_but_its_type():
    assert parse_command('{"type": "status"}') == Command(STATUS)


def test_a_cancel_carries_the_turn_it_names():
    assert parse_command('{"type": "cancel", "turn": "t-3", "ref": 7}') == Command(
        CANCEL, 7, turn="t-3"
    )


@pytest.mark.parametrize(
    "line, reason",
    [
        ("not json", "stdin line is not JSON"),
        ('["submit"]', "stdin line is not a JSON object"),
        ('{"type": "submit", "text": "   "}', "submit needs a non-empty 'text'"),
        ('{"type": "submit"}', "submit needs a non-empty 'text'"),
        ('{"type": "cancel"}', "cancel needs a 'turn' id"),
        ('{"type": "cancel", "turn": 4}', "cancel needs a 'turn' id"),
        ('{"type": "steer"}', "unknown command type: 'steer'"),
    ],
)
def test_a_line_that_cannot_be_acted_on_is_refused_with_its_reason(line, reason):
    assert parse_command(line) == Rejection(reason)


def test_a_refusal_keeps_the_ref_so_the_client_can_attribute_it():
    assert parse_command('{"type": "submit", "ref": "c-9"}').ref == "c-9"


def test_tagging_echoes_the_ref_and_leaves_untagged_events_alone():
    assert tagged([{"type": "ack"}], "c-2") == [{"type": "ack", "ref": "c-2"}]
    assert tagged([{"type": "ack"}], None) == [{"type": "ack"}]
