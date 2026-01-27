from __future__ import annotations

from dataclasses import dataclass
import asyncio


@dataclass
class InflightTextStream:
    queue: asyncio.Queue[str]
    accepting: bool = True
