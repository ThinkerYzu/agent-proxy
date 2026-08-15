"""Multi-line string content -> backslash+CR soft newlines, single CR submit.

The TUI grammar (OQ-5): inside bracketed paste, a literal backslash followed
by CR inserts a newline in the input panel without submitting; a bare CR
submits. So an outbound caller's "\\n" becomes "\\\\\\r", and the encoded
payload ends with exactly one trailing CR that submits.
"""

from agentproxy.input_encoder import (
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    SOFT_NEWLINE,
    SUBMIT,
    encode_user_message,
)


def test_two_line_content():
    msg = {"type": "user", "message": {"role": "user", "content": "line1\nline2"}}
    assert (
        encode_user_message(msg)
        == BRACKETED_PASTE_START
        + b"line1"
        + SOFT_NEWLINE
        + b"line2"
        + BRACKETED_PASTE_END
        + SUBMIT
    )


def test_trailing_newline_preserved_as_soft_newline():
    # A trailing "\n" becomes a trailing soft-newline INSIDE the bracketed
    # paste — followed by the closing 201~ and then the single submit CR.
    msg = {"type": "user", "message": {"role": "user", "content": "line1\n"}}
    assert (
        encode_user_message(msg)
        == BRACKETED_PASTE_START
        + b"line1"
        + SOFT_NEWLINE
        + BRACKETED_PASTE_END
        + SUBMIT
    )


def test_only_one_submitting_cr():
    # No matter how many "\n"s we have inside, exactly one trailing CR
    # follows the bracketed-paste end marker — that's the keystroke that
    # actually submits.
    msg = {
        "type": "user",
        "message": {"role": "user", "content": "a\nb\nc\nd"},
    }
    encoded = encode_user_message(msg)
    assert encoded.endswith(BRACKETED_PASTE_END + SUBMIT)
    after_end = encoded[encoded.rindex(BRACKETED_PASTE_END) + len(BRACKETED_PASTE_END):]
    assert after_end == SUBMIT
    # Three "\n"s in the input -> three soft-newlines in the payload.
    body = encoded[len(BRACKETED_PASTE_START):encoded.rindex(BRACKETED_PASTE_END)]
    assert body.count(SOFT_NEWLINE) == 3
