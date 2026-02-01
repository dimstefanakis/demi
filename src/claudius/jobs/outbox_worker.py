from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
import sqlite3
from typing import Any

from claudius.db.core import Database


@dataclass
class OutboxWorkerConfig:
    poll_interval: float = 1.0
    batch_size: int = 50


@dataclass
class OutboxWorker:
    db: Database
    messenger: Any
    config: OutboxWorkerConfig
    _running: bool = False

    async def _db_call(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def run_forever(self) -> None:
        self._running = True
        try:
            while self._running:
                try:
                    rows = await self._db_call(self.db.claim_outbox, self.config.batch_size)
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc).lower():
                        await asyncio.sleep(self.config.poll_interval)
                        continue
                    raise
                if not rows:
                    await asyncio.sleep(self.config.poll_interval)
                    continue
                sent_ids: list[str] = []
                failed_ids: list[str] = []
                for row in rows:
                    payload = row.get("payload_json") if isinstance(row, dict) else None
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except json.JSONDecodeError:
                            payload = {}
                    if not isinstance(payload, dict):
                        payload = {}
                    text = str(payload.get("text") or "").strip()
                    tenant_external_id = str(payload.get("tenant_external_id") or "").strip()
                    if not text or not tenant_external_id:
                        failed_ids.append(str(row.get("id")))
                        continue
                    try:
                        await self.messenger.send_text(tenant_external_id, text)
                        sent_ids.append(str(row.get("id")))
                    except Exception:
                        failed_ids.append(str(row.get("id")))
                if sent_ids:
                    await self._db_call(self.db.update_outbox_statuses, sent_ids, "sent")
                if failed_ids:
                    await self._db_call(self.db.update_outbox_statuses, failed_ids, "failed")
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False
