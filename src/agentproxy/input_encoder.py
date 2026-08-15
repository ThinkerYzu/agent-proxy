"""Pure encoding from a parsed user-message dict to pty bytes.

The inner `claude` TUI accepts user input as a bracketed-paste sequence
(``ESC [ 200 ~`` ... ``ESC [ 201 ~``) followed by a single ``\\r`` to submit.
Embedded newlines map to ``\\\\\\r`` — backslash-then-CR is the TUI's
soft-newline grammar.

v1 supports only string-typed ``content``. Structured content-block lists
(e.g. images, tool inputs) are explicitly rejected with ``ValueError`` so the
caller sees a clear failure rather than silent message loss.
"""

from __future__ import annotations


BRACKETED_PASTE_START = b"\x1b[200~"
BRACKETED_PASTE_END = b"\x1b[201~"
SOFT_NEWLINE = b"\\\r"
SUBMIT = b"\r"

# Empirical: with `--dangerously-skip-permissions` the inner claude eats the
# submit CR when it arrives in the same `os.write()` as the bracketed-paste
# end marker. Splitting into two writes with this delay between paste-end
# and submit is enough to land the submit reliably. The encoder produces
# (paste_block, submit_cr) chunks so the transport can stage them.
SUBMIT_DELAY = 0.05


def encode_user_message_chunks(msg: dict) -> tuple[bytes, bytes]:
    """Encode to two chunks: the bracketed-paste block, and the submit CR.

    The transport writes them with `SUBMIT_DELAY` between, so the inner TUI
    has a moment to exit paste mode before processing the submit keystroke.
    """
    text = _extract_text(msg)
    payload = text.encode("utf-8").replace(b"\n", SOFT_NEWLINE)
    return (BRACKETED_PASTE_START + payload + BRACKETED_PASTE_END, SUBMIT)


def encode_user_message(msg: dict) -> bytes:
    """Encode a stream-json user message to its full pty wire-bytes.

    Returns the concatenation of `encode_user_message_chunks(msg)`; useful for
    byte-equality tests of the wire format. Live transport should use
    `encode_user_message_chunks` and the `SUBMIT_DELAY` between writes.
    """
    paste, submit = encode_user_message_chunks(msg)
    return paste + submit


def _extract_text(msg: object) -> str:
    if not isinstance(msg, dict):
        raise ValueError("unsupported input shape: expected a JSON object")
    if msg.get("type") != "user":
        raise ValueError(
            f"unsupported input shape: type must be 'user' (got {msg.get('type')!r})"
        )
    message = msg.get("message")
    if not isinstance(message, dict):
        raise ValueError("unsupported input shape: missing 'message' object")
    content = message.get("content")
    if isinstance(content, str):
        return content
    raise ValueError(
        "unsupported input shape: only string 'content' is supported in v1 "
        "(structured content-block lists are not yet implemented)"
    )
