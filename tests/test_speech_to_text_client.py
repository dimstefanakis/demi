from __future__ import annotations

import httpx
import pytest

from demi.messaging.speech import OpenAISpeechToTextClient, OpenAISpeechToTextConfig


@pytest.mark.asyncio
async def test_openai_speech_to_text_client_posts_transcription_request(tmp_path):
    captured: dict[str, object] = {}
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"fake-audio")

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["content_type"] = request.headers.get("Content-Type")
        captured["body"] = request.content
        return httpx.Response(200, json={"text": "transcribed text"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAISpeechToTextClient(
            OpenAISpeechToTextConfig(
                api_key="test-key",
                model="gpt-4o-transcribe",
                language="en",
            ),
            http_client=http_client,
        )
        text = await client.transcribe_file(audio_path)

    assert text == "transcribed text"
    assert str(captured["url"]).endswith("/audio/transcriptions")
    assert captured["authorization"] == "Bearer test-key"
    assert str(captured["content_type"]).startswith("multipart/form-data")
    body = bytes(captured["body"])
    assert b'name="model"' in body
    assert b"gpt-4o-transcribe" in body
    assert b'name="language"' in body
    assert b"voice.ogg" in body


@pytest.mark.asyncio
async def test_openai_speech_to_text_client_returns_none_for_missing_text(tmp_path):
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"fake-audio")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": ""}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAISpeechToTextClient(
            OpenAISpeechToTextConfig(api_key="test-key"),
            http_client=http_client,
        )
        text = await client.transcribe_file(audio_path)

    assert text is None


@pytest.mark.asyncio
async def test_openai_speech_to_text_client_normalizes_oga_filename(tmp_path):
    captured: dict[str, object] = {}
    audio_path = tmp_path / "voice.oga"
    audio_path.write_bytes(b"fake-audio")

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200, json={"text": "ok"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = OpenAISpeechToTextClient(
            OpenAISpeechToTextConfig(api_key="test-key"),
            http_client=http_client,
        )
        text = await client.transcribe_file(audio_path)

    assert text == "ok"
    body = bytes(captured["body"])
    assert b'filename="voice.ogg"' in body
    assert b'filename="voice.oga"' not in body
