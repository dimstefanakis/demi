from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass
class OpenAISpeechToTextConfig:
    api_key: str
    model: str = "gpt-4o-transcribe"
    timeout_seconds: float = 45.0
    base_url: str = "https://api.openai.com/v1"
    language: str | None = None


class OpenAISpeechToTextClient:
    def __init__(
        self,
        config: OpenAISpeechToTextConfig,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.config = config
        self._client = http_client or httpx.AsyncClient(timeout=config.timeout_seconds)

    @staticmethod
    def _upload_filename(file_path: Path) -> str:
        suffix = file_path.suffix.lower()
        if suffix in {".oga", ".opus"}:
            return f"{file_path.stem}.ogg"
        return file_path.name

    async def transcribe_file(self, file_path: Path) -> str | None:
        if not file_path.exists() or not file_path.is_file():
            return None

        url = f"{self.config.base_url.rstrip('/')}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        data: dict[str, Any] = {"model": self.config.model}
        language = str(self.config.language or "").strip()
        if language:
            data["language"] = language

        with file_path.open("rb") as file_handle:
            files = {"file": (self._upload_filename(file_path), file_handle)}
            response = await self._client.post(url, headers=headers, data=data, files=files)

        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        text = str(payload.get("text") or "").strip()
        return text or None
