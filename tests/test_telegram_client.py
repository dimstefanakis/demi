from __future__ import annotations

import json

import httpx
import pytest

from demi.messaging.telegram import TelegramClient, TelegramConfig


@pytest.mark.asyncio
async def test_send_text_accepts_successful_telegram_response():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 123}},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramClient(TelegramConfig(bot_token="token"), http_client=http_client)
        await client.send_text("42", "hello", reply_to_message_id="7")

    assert str(captured["url"]).endswith("/sendMessage")
    assert captured["payload"] == {"chat_id": "42", "text": "hello", "reply_to_message_id": 7}


@pytest.mark.asyncio
async def test_send_text_raises_on_telegram_ok_false_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramClient(TelegramConfig(bot_token="token"), http_client=http_client)
        with pytest.raises(RuntimeError, match="telegram_send_failed"):
            await client.send_text("42", "hello")


@pytest.mark.asyncio
async def test_send_text_raises_on_invalid_json_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramClient(TelegramConfig(bot_token="token"), http_client=http_client)
        with pytest.raises(RuntimeError, match="telegram_send_invalid_json"):
            await client.send_text("42", "hello")


@pytest.mark.asyncio
async def test_send_text_raises_for_http_error_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"ok": False, "description": "Too Many Requests"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TelegramClient(TelegramConfig(bot_token="token"), http_client=http_client)
        with pytest.raises(httpx.HTTPStatusError):
            await client.send_text("42", "hello")
