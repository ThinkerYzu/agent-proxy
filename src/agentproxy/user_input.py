"""Identified questions, independent of an agent's transport and turn clocks."""
from __future__ import annotations

import copy
import json
from collections import deque
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

DELIVERY_TIMEOUT_S = 30.0


def input_timeout() -> float:
    try:
        value = float(os.environ.get("AGENTPROXY_INPUT_TIMEOUT_S", "86400"))
    except ValueError as exc:
        raise ValueError("AGENTPROXY_INPUT_TIMEOUT_S must be positive and finite") from exc
    # Also bound the value to what datetime and hook timers can represent.
    if not math.isfinite(value) or not 0 < value <= 31536000:
        raise ValueError("AGENTPROXY_INPUT_TIMEOUT_S must be positive, finite and at most 31536000")
    return value


def normalise_questions(raw: object, agent: str) -> tuple[list[dict], list[str]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("input request needs questions")
    questions, native_ids = [], []
    for i, q in enumerate(raw, 1):
        if not isinstance(q, dict) or not isinstance(q.get("question"), str) or not q["question"].strip():
            raise ValueError("input question needs non-empty text")
        if not isinstance(q.get("header", ""), str) or any(
            not isinstance(q.get(field, False), bool)
            for field in (("multiSelect",) if agent == "claude" else ("isOther", "isSecret"))
        ):
            raise ValueError("input question has invalid header or selection flags")
        native_id = q["question"] if agent == "claude" else q.get("id")
        if not isinstance(native_id, str) or not native_id or native_id in native_ids:
            raise ValueError("input question identities must be distinct non-empty strings")
        native_ids.append(native_id)
        options, labels = [], set()
        raw_options = q.get("options")
        if raw_options is None:
            raw_options = []
        if not isinstance(raw_options, list):
            raise ValueError("input options must be an array")
        for j, option in enumerate(raw_options, 1):
            if not isinstance(option, dict) or not isinstance(option.get("label"), str) or not option["label"].strip():
                raise ValueError("input option needs a non-empty label")
            label = option["label"]
            if label in labels:
                raise ValueError("input option labels must be distinct")
            labels.add(label)
            description = option.get("description", "")
            if not isinstance(description, str):
                raise ValueError("input option description must be text")
            options.append({"id": f"o-{j}", "label": label, "description": description})
        questions.append({
            "id": f"q-{i}", "header": str(q.get("header", "")), "question": q["question"],
            "options": options, "multi_select": bool(q.get("multiSelect", False)) if agent == "claude" else False,
            "allow_text": True if agent == "claude" else bool(q.get("isOther", False)) or not options,
            "secret": bool(q.get("isSecret", False)),
        })
    return questions, native_ids


@dataclass
class InputRequest:
    id: str
    key: object
    turn: dict
    questions: list[dict]
    native_ids: list[str]
    blocking: bool
    deadline: float
    expires_at: str
    deliver: Callable[[dict | None, str], None]
    state: str = "pending"
    answering_at: float | None = None
    metadata: dict = field(default_factory=dict)

    def wire(self) -> dict:
        return copy.deepcopy({**self.metadata, "id": self.id, "blocking": self.blocking,
                              "expires_at": self.expires_at, "questions": self.questions})


class InputRequests:
    def __init__(self, timeout_s: float = 86400, clock=time.monotonic, wall_clock=time.time):
        if not math.isfinite(timeout_s) or not 0 < timeout_s <= 31536000:
            raise ValueError("input timeout must be positive, finite and at most 31536000")
        self.timeout_s = timeout_s
        self.clock, self.wall_clock = clock, wall_clock
        self.requests: dict[str, InputRequest] = {}
        self._seq = 0
        self._seen = deque(maxlen=1024)

    def count(self) -> int:
        return len(self.requests)

    def snapshot(self) -> list[dict]:
        return [{**r.wire(), "turn": dict(r.turn), "state": r.state} for r in self.requests.values()]

    def get_key(self, key: object) -> InputRequest | None:
        return next((r for r in self.requests.values() if r.key == key), None)

    def seen(self, key) -> bool:
        return key in self._seen or self.get_key(key) is not None

    def add(self, key, turn, raw, agent, blocking, deliver, metadata=None) -> dict | None:
        if self.seen(key):
            return None
        questions, native_ids = normalise_questions(raw, agent)
        self._seq += 1
        request = InputRequest(
            f"i-{self._seq}", key, dict(turn), questions, native_ids, blocking,
            self.clock() + self.timeout_s,
            datetime.fromtimestamp(self.wall_clock() + self.timeout_s, timezone.utc).isoformat(), deliver,
        )
        self.requests[request.id] = request
        request.metadata = metadata or {}
        self._seen.append(key)
        return {"type": "input_request", "turn": request.turn, "request": request.wire()}

    def add_permission(self, key, turn, details, deliver, *, allow=True):
        """A permission answer can select only an offered operation decision."""
        options = ([{"label": "Allow once", "description": "Approve only this operation"}] if allow else [])
        options.append({"label": "Deny", "description": "Do not approve this operation"})
        # Put the operation in question text as well as structured metadata so
        # existing choice renderers can show exactly what is being approved.
        operation = details.get("operation", details.get("input", {}))
        tool = details.get("tool") or ("file changes" if "fileChange" in details.get("method", "") else "command")
        description = f"Allow {tool} once?"
        if isinstance(operation, dict):
            if isinstance(operation.get("command"), str):
                description += "\n\n" + operation["command"]
            else:
                description += "\n\n" + json.dumps(operation, indent=2, ensure_ascii=False)
            cwd = operation.get("cwd", details.get("cwd"))
            if cwd:
                description += f"\nDirectory: {cwd}"
            for field in ("reason", "additionalPermissions", "networkApprovalContext"):
                if operation.get(field) is not None:
                    description += f"\n{field}: " + json.dumps(operation[field], ensure_ascii=False)
        item = details.get("item") or {}
        if item.get("changes") is not None:
            description += "\nChanges: " + json.dumps(item["changes"], indent=2, ensure_ascii=False)
        return self.add(key, turn, [{"id": "permission", "header": "Permission",
            "question": description, "options": options}], "codex", True, deliver,
            {"kind": "permission", "permission": {**details, "scope": "operation"}})

    def accept(self, request_id: str, answers: object, dismiss: bool) -> tuple[InputRequest, dict | None]:
        r = self.requests.get(request_id)
        if r is None:
            raise ValueError("unknown or resolved input request")
        if r.state != "pending":
            raise ValueError("input request already has an accepted answer")
        if self.clock() >= r.deadline:
            raise ValueError("input request has expired")
        native = None
        if not dismiss:
            if not isinstance(answers, dict) or set(answers) != {q["id"] for q in r.questions}:
                raise ValueError("answer must supply exactly every question in the request")
            native = {}
            for q, native_id in zip(r.questions, r.native_ids):
                value = answers[q["id"]]
                if not isinstance(value, dict):
                    raise ValueError("each answer must contain choices or text")
                if set(value) == {"text"}:
                    text = value["text"]
                    if not q["allow_text"] or not isinstance(text, str) or not text.strip():
                        raise ValueError("this question needs an allowed non-empty text answer")
                    selected = [text]
                elif set(value) == {"choices"}:
                    choices = value["choices"]
                    options = {o["id"]: o["label"] for o in q["options"]}
                    if (not isinstance(choices, list) or not choices
                        or any(not isinstance(c, str) for c in choices)
                        or len(set(choices)) != len(choices)
                        or any(c not in options for c in choices)
                        or (not q["multi_select"] and len(choices) != 1)):
                        raise ValueError("invalid selection for input question")
                    selected = [options[c] for c in choices]
                else:
                    raise ValueError("each answer needs choices or text, exclusively")
                native[native_id] = selected
        r.state = "answering"
        r.answering_at = self.clock()
        return r, native

    def send(self, r: InputRequest, native: dict | None, reason: str = "answered") -> list[dict]:
        r.deliver(native, reason)
        return [] if reason == "answered" else self.resolve(r.key, reason)

    def resolve(self, key: object, reason: str | None = None) -> list[dict]:
        r = self.get_key(key)
        if r is None:
            return []
        del self.requests[r.id]
        return [{"type": "input_resolved", "turn": r.turn, "request": r.id,
                 "reason": reason or ("answered" if r.state == "answering" else "backend_closed")}]

    def close(self, reason: str, turn_id: str | None = None) -> list[dict]:
        events = []
        for r in list(self.requests.values()):
            if turn_id is not None and r.turn["id"] != turn_id:
                continue
            if r.state == "pending" and reason != "error":
                r.deliver(None, reason)
            events += self.resolve(r.key, reason)
        return events

    def tick(self) -> list[dict]:
        events = []
        for r in list(self.requests.values()):
            if r.state == "pending" and self.clock() >= r.deadline:
                r.deliver(None, "expired")
                events += self.resolve(r.key, "expired")
            elif r.state == "answering" and self.clock() - r.answering_at >= DELIVERY_TIMEOUT_S:
                raise RuntimeError("input answer delivery was not confirmed within 30 seconds")
        return events

    def blocks(self, turn_id: str) -> bool:
        return any(r.turn["id"] == turn_id and r.blocking and r.state == "pending"
                   for r in self.requests.values())
