from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json

from demi.models import Attachment


@dataclass
class FileMessenger:
    tasks_dir: Path

    async def send_text(self, chat_id: str, text: str) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        path = self.tasks_dir / "outbound_messages.jsonl"
        payload = {"chat_id": chat_id, "text": text}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    async def download_images(self, images: list[Attachment], dest_dir: Path) -> list[str]:
        return []
