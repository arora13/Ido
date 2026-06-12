from io import BytesIO
import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from adapters.blender.ido_blender.client import BackendClient, BackendError, SSEDecoder


def frame(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


def test_sse_decoder_handles_chunked_and_multiple_events() -> None:
    decoder = SSEDecoder()
    payload = frame({"type": "status", "message": "Generating"}) + frame(
        {"type": "done", "result": {"status": "ok"}}
    )

    events = []
    events.extend(decoder.feed(payload[:7]))
    events.extend(decoder.feed(payload[7:31]))
    events.extend(decoder.feed(payload[31:]))
    events.extend(decoder.finish())

    assert [event["type"] for event in events] == ["status", "done"]


def test_sse_decoder_supports_multiline_data() -> None:
    decoder = SSEDecoder()

    events = decoder.feed(b'data: {"type":"status",\ndata: "message":"Ready"}\n\n')

    assert events == [{"type": "status", "message": "Ready"}]


def test_sse_decoder_rejects_invalid_json() -> None:
    decoder = SSEDecoder()

    with pytest.raises(BackendError, match="invalid stream event"):
        decoder.feed(b"data: not-json\n\n")


def test_prompt_stream_rejects_eof_before_final_event() -> None:
    response = BytesIO(frame({"type": "status", "message": "Generating"}))

    with patch("adapters.blender.ido_blender.client.urlopen", return_value=response):
        with pytest.raises(BackendError, match="ended before a final result"):
            BackendClient("http://example.test").prompt_stream(
                "make a chair",
                None,
                lambda _event: None,
            )


def test_prompt_stream_reports_http_failure() -> None:
    error = HTTPError(
        "http://example.test/api/prompt/stream",
        500,
        "error",
        {},
        BytesIO(b"provider crashed"),
    )

    with patch("adapters.blender.ido_blender.client.urlopen", side_effect=error):
        with pytest.raises(BackendError, match="HTTP 500: provider crashed"):
            BackendClient("http://example.test").prompt_stream(
                "make a chair",
                None,
                lambda _event: None,
            )
