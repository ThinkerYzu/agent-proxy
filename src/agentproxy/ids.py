"""Synthetic id generation for the wrapper-owned identifier fields.

The wrapper has no API-level visibility, so every `session_id`, `uuid`,
`msg_…`, `toolu_…`, and `req_…` it writes to stdout is locally generated.
These IDs need only be unique within the stream — the caller treats them
opaquely. We mimic the *shape* of the real Anthropic ids so callers that
length-validate the string don't trip over the wrapper's output.

Tests pin determinism by monkey-patching the module-level functions
(`make_session_id`, `make_uuid`, `make_msg_id`, `make_toolu_id`,
`make_request_id`) via the `fixed_ids` fixture; callers must access these
through the module (`from agentproxy import ids; ids.make_uuid()`) for the
patch to take effect.
"""

from __future__ import annotations

import secrets
import uuid


_BASE32_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford-ish, no I/L/O/U


def _base32(length: int) -> str:
    return "".join(_BASE32_ALPHABET[b % 32] for b in secrets.token_bytes(length))


def make_session_id() -> str:
    return str(uuid.uuid4())


def make_uuid() -> str:
    return str(uuid.uuid4())


def make_msg_id() -> str:
    # Real shape: msg_01<22 base32 chars> (24 chars after underscore).
    return "msg_01" + _base32(22)


def make_toolu_id() -> str:
    return "toolu_01" + _base32(22)


def make_request_id() -> str:
    return "req_01" + _base32(22)
