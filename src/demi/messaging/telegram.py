from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from demi.models import Attachment, NormalizedMessage


class TelegramUpdateParser:
    @staticmethod
    def parse(update: dict[str, Any]) -> NormalizedMessage | None:
        message = update.get("message")
        if not message:
            return None

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return None

        message_id = message.get("message_id")
        if message_id is None:
            return None

        timestamp = message.get("date")
        received_at = datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp else datetime.now(tz=timezone.utc)

        text = message.get("text") or message.get("caption")

        images: list[Attachment] = []
        for photo in message.get("photo") or []:
            images.append(
                Attachment(
                    provider_file_id=photo.get("file_id"),
                    width=photo.get("width"),
                    height=photo.get("height"),
                )
            )

        animation = message.get("animation")
        if animation:
            images.append(
                Attachment(
                    provider_file_id=animation.get("file_id"),
                    width=animation.get("width"),
                    height=animation.get("height"),
                )
            )

        document = message.get("document")
        if document:
            images.append(
                Attachment(
                    provider_file_id=document.get("file_id"),
                    width=document.get("width"),
                    height=document.get("height"),
                )
            )

        video = message.get("video")
        if video:
            images.append(
                Attachment(
                    provider_file_id=video.get("file_id"),
                    width=video.get("width"),
                    height=video.get("height"),
                )
            )

        return NormalizedMessage(
            provider="telegram",
            provider_message_id=str(message_id),
            tenant_external_id=str(chat_id),
            received_at=received_at,
            text=text,
            images=images,
            raw=update,
        )


def message_has_reply(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    message = raw.get("message") or raw.get("edited_message")
    if not isinstance(message, dict):
        if "reply_to_message" in raw:
            message = raw
        else:
            return False
    return bool(message.get("reply_to_message"))


@dataclass
class TelegramConfig:
    bot_token: str


class TelegramClient:
    def __init__(self, config: TelegramConfig, http_client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = http_client or httpx.AsyncClient(timeout=15)

    async def send_text(
        self, chat_id: str, text: str, reply_to_message_id: str | None = None
    ) -> None:
        url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id:
            try:
                payload["reply_to_message_id"] = int(reply_to_message_id)
            except (TypeError, ValueError):
                pass
        await self._client.post(url, json=payload)

    async def download_images(self, images: list[Attachment], dest_dir: Path) -> list[str]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        saved_paths: list[str] = []

        for image in images:
            file_info_url = f"https://api.telegram.org/bot{self.config.bot_token}/getFile"
            info_resp = await self._client.get(file_info_url, params={"file_id": image.provider_file_id})
            info_resp.raise_for_status()
            info = info_resp.json()
            file_path = info.get("result", {}).get("file_path")
            if not file_path:
                continue

            file_url = f"https://api.telegram.org/file/bot{self.config.bot_token}/{file_path}"
            file_resp = await self._client.get(file_url)
            file_resp.raise_for_status()

            suffix = Path(file_path).suffix or ".jpg"
            dest_path = dest_dir / f"{image.provider_file_id}{suffix}"
            dest_path.write_bytes(file_resp.content)
            saved_paths.append(str(dest_path))

        return saved_paths
