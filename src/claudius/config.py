from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    root_dir: Path = Path(".")
    data_dir: Path = Path("data")
    db_path: Path | None = None

    telegram_bot_token: str | None = None

    gemini_cmd: str = "gemini"
    design_prompt_path: Path = Path("DESIGN.md")

    vercel_cmd: str = "vercel"
    vercel_token: str | None = None
    vercel_scope: str | None = None

    unsplash_app_id: str | None = None
    unsplash_access_key: str | None = None
    unsplash_secret_key: str | None = None

    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_success_url: str | None = None
    stripe_cancel_url: str | None = None

    def resolved_data_dir(self) -> Path:
        return (self.root_dir / self.data_dir).resolve()

    def resolved_db_path(self) -> Path:
        if self.db_path:
            return self.db_path
        return self.resolved_data_dir() / "claudius.sqlite"

    def resolved_gemini_cmd(self) -> str:
        local_cmd = (self.root_dir / "node_modules" / ".bin" / "gemini").resolve()
        return str(local_cmd) if local_cmd.exists() else self.gemini_cmd

    def resolved_vercel_cmd(self) -> str:
        local_cmd = (self.root_dir / "node_modules" / ".bin" / "vercel").resolve()
        return str(local_cmd) if local_cmd.exists() else self.vercel_cmd
