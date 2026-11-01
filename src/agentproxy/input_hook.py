"""Claude command-hook helper. stdout is reserved for Claude's hook JSON."""
from __future__ import annotations

import json
import socket
import sys

from agentproxy.input_bridge import MAX_MESSAGE_BYTES


def denial(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
            "permissionDecision": "deny", "permissionDecisionReason": reason}}


def main() -> int:
    payload = None
    try:
        raw = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ValueError("question exceeds hook message limit")
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("tool_name") != "AskUserQuestion":
            raise ValueError("unexpected hook tool")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(sys.argv[1])
            connection.sendall(json.dumps(payload).encode() + b"\n")
            with connection.makefile("rb") as response:
                line = response.readline(MAX_MESSAGE_BYTES + 1)
            if not line or len(line) > MAX_MESSAGE_BYTES:
                raise ValueError("question bridge closed without a response")
            output = json.loads(line)
    except (OSError, ValueError, IndexError) as exc:
        if isinstance(payload, dict) and payload.get("hook_event_name") != "PreToolUse":
            output = {}
        else:
            output = denial(f"agent-proxy question unavailable: {exc}")
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
