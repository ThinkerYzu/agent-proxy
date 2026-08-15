"""Unsupported input shapes raise ValueError with an explicit message.

v1 of the wrapper accepts only string-typed ``content`` in the user
message. Anything else — content-block lists (images), wrong type,
missing message — is an explicit ValueError so the caller cannot silently
lose data.
"""

import pytest

from agentproxy.input_encoder import encode_user_message


def test_content_block_list_raises():
    msg = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "hello"}],
        },
    }
    with pytest.raises(ValueError, match="unsupported input shape"):
        encode_user_message(msg)


def test_image_content_block_raises():
    msg = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "xxx"},
                }
            ],
        },
    }
    with pytest.raises(ValueError, match="unsupported input shape"):
        encode_user_message(msg)


def test_missing_message_raises():
    with pytest.raises(ValueError, match="unsupported input shape"):
        encode_user_message({"type": "user"})


def test_wrong_top_level_type_raises():
    with pytest.raises(ValueError, match="unsupported input shape"):
        encode_user_message(
            {"type": "assistant", "message": {"role": "assistant", "content": "x"}}
        )


def test_non_dict_raises():
    with pytest.raises(ValueError, match="unsupported input shape"):
        encode_user_message("just a string")  # type: ignore[arg-type]


def test_none_content_raises():
    with pytest.raises(ValueError, match="unsupported input shape"):
        encode_user_message({"type": "user", "message": {"role": "user", "content": None}})
