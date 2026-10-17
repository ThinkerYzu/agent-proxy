"""The JSON-RPC wire to a `codex app-server`, with no server at all."""

from __future__ import annotations

import json

import pytest

from agentproxy.app_server import (
    AppServerError,
    AppServerPeer,
    Notification,
    Response,
    ServerRequest,
)


class Wire:
    """Captures what the peer writes, one JSON object per line."""

    def __init__(self) -> None:
        self.data = b""

    def __call__(self, data: bytes) -> None:
        self.data += data

    @property
    def sent(self) -> list[dict]:
        return [json.loads(line) for line in self.data.splitlines()]


@pytest.fixture
def wire() -> Wire:
    return Wire()


@pytest.fixture
def peer(wire) -> AppServerPeer:
    return AppServerPeer(wire)


def _line(message: dict) -> bytes:
    return json.dumps(message).encode() + b"\n"


def test_a_request_is_numbered_and_sent_without_a_jsonrpc_member(peer, wire):
    assert peer.request("thread/start", {"cwd": "/w"}) == 1
    assert peer.request("turn/start") == 2
    assert wire.sent == [
        {"method": "thread/start", "id": 1, "params": {"cwd": "/w"}},
        {"method": "turn/start", "id": 2},
    ]
    assert peer.pending == {1: "thread/start", 2: "turn/start"}


def test_a_notification_and_an_answer_carry_no_new_id(peer, wire):
    peer.notify("initialized")
    peer.respond(0, {"decision": "decline"})
    assert wire.sent == [{"method": "initialized"}, {"id": 0, "result": {"decision": "decline"}}]


def test_a_response_is_matched_to_the_method_it_answers(peer):
    peer.request("turn/start")
    (message,) = peer.feed(_line({"id": 1, "result": {"turn": {"id": "u1"}}}))
    assert message == Response(1, "turn/start", {"turn": {"id": "u1"}}, None)
    assert peer.pending == {}


def test_an_error_response_keeps_its_error(peer):
    peer.request("thread/resume")
    (message,) = peer.feed(_line({"id": 1, "error": {"code": -32600, "message": "no thread"}}))
    assert message.error == {"code": -32600, "message": "no thread"}


def test_notifications_and_server_requests_are_told_apart(peer):
    messages = peer.feed(
        _line({"method": "turn/started", "params": {"threadId": "a"}})
        + _line({"id": 0, "method": "item/fileChange/requestApproval", "params": {}})
    )
    assert messages == [
        Notification("turn/started", {"threadId": "a"}),
        ServerRequest(0, "item/fileChange/requestApproval", {}),
    ]


def test_a_partial_line_waits_for_the_rest(peer):
    data = _line({"method": "turn/started", "params": {}})
    assert peer.feed(data[:10]) == []
    assert peer.feed(data[10:]) == [Notification("turn/started", {})]


def test_lines_that_are_not_messages_are_skipped_not_fatal(peer):
    messages = peer.feed(
        b"not json\n[1, 2]\n\n"
        + _line({"id": 9, "result": {}})
        + _line({"params": {}})
        + _line({"method": "warning", "params": {"message": "x"}})
    )
    assert messages == [Notification("warning", {"message": "x"})]


def test_a_blocking_call_returns_its_result_and_keeps_what_came_before_it(peer):
    chunks = [
        _line({"method": "account/updated", "params": {"authMode": "chatgpt"}}),
        b"",
        _line({"id": 1, "result": {"account": {"type": "chatgpt"}}}),
    ]
    result = peer.call("account/read", {}, read=lambda _t: chunks.pop(0), timeout=5)
    assert result == {"account": {"type": "chatgpt"}}
    assert peer.feed(b"") == [Notification("account/updated", {"authMode": "chatgpt"})]


def test_a_blocking_call_raises_on_an_error_answer(peer):
    chunks = [_line({"id": 1, "error": {"message": "thread not found"}})]
    with pytest.raises(AppServerError, match="thread not found"):
        peer.call("thread/resume", {}, read=lambda _t: chunks.pop(0), timeout=5)


def test_a_blocking_call_raises_when_the_server_closes(peer):
    with pytest.raises(AppServerError, match="closed"):
        peer.call("initialize", {}, read=lambda _t: None, timeout=5)


def test_a_blocking_call_gives_up_at_its_deadline(peer):
    now = [0.0]

    def read(_timeout):
        now[0] += 3
        return b""

    with pytest.raises(AppServerError, match="no answer"):
        peer.call("initialize", {}, read=read, timeout=5, clock=lambda: now[0])
