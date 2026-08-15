"""Single-line string content -> bracketed-paste + CR submit."""

from agentproxy.input_encoder import (
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    SUBMIT,
    encode_user_message,
)


def test_simple_singleline():
    msg = {"type": "user", "message": {"role": "user", "content": "Reply PONG"}}
    assert (
        encode_user_message(msg)
        == BRACKETED_PASTE_START + b"Reply PONG" + BRACKETED_PASTE_END + SUBMIT
    )


def test_singleline_utf8_payload():
    # Non-ASCII payload survives unmodified inside the bracketed-paste envelope.
    msg = {"type": "user", "message": {"role": "user", "content": "héllo ✓"}}
    expected = (
        BRACKETED_PASTE_START
        + "héllo ✓".encode("utf-8")
        + BRACKETED_PASTE_END
        + SUBMIT
    )
    assert encode_user_message(msg) == expected


def test_singleline_with_internal_backslashes_passthrough():
    # A literal backslash (not followed by newline) is not special — it just
    # passes through. Only "\n" gets rewritten.
    msg = {"type": "user", "message": {"role": "user", "content": r"path/with\backslash"}}
    encoded = encode_user_message(msg)
    assert encoded.startswith(BRACKETED_PASTE_START)
    assert encoded.endswith(BRACKETED_PASTE_END + SUBMIT)
    body = encoded[len(BRACKETED_PASTE_START):-len(BRACKETED_PASTE_END + SUBMIT)]
    assert body == rb"path/with\backslash"
