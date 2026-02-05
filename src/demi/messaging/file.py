from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json

from demi.models import Attachment


@dataclass
class FileMessenger:
    tasks_dir: Path

    async def send_text(
        self, chat_id: str, text: str, reply_to_message_id: str | None = None
    ) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        path = self.tasks_dir / "outbound_messages.jsonl"
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    async def download_images(self, images: list[Attachment], dest_dir: Path) -> list[str]:
        return []
