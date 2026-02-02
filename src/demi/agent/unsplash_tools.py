from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from pathlib import Path

import httpx

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool

from demi.agent.tool_logging import log_tool_run

UNSPLASH_SERVER_NAME = "demi-unsplash"
UNSPLASH_SEARCH_TOOL = f"mcp__{UNSPLASH_SERVER_NAME}__search_photos"


@dataclass(frozen=True)
class UnsplashToolContext:
    access_key: str | None
    app_name: str = "demi"
    tasks_dir: Path | None = None


def build_unsplash_server(context: UnsplashToolContext) -> McpSdkServerConfig:
    import time

    @tool(
        "search_photos",
        "Search Unsplash for photos and return a small list of image URLs.",
        {"query": str, "count": int, "orientation": str},
    )
    async def search_photos(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        access_key = context.access_key
        if not access_key:
            log_tool_run(
                getattr(context, "tasks_dir", Path.cwd()),
                "search_photos",
                args=args,
                result={"ok": False, "error": "UNSPLASH_ACCESS_KEY is not set."},
                duration_ms=(time.monotonic() - start) * 1000.0,
            )
            return _error("UNSPLASH_ACCESS_KEY is not set.")

        query = str(args.get("query", "")).strip()
        if not query:
            log_tool_run(
                getattr(context, "tasks_dir", Path.cwd()),
                "search_photos",
                args=args,
                result={"ok": False, "error": "query is required."},
                duration_ms=(time.monotonic() - start) * 1000.0,
            )
            return _error("query is required.")

        count = _clamp_int(args.get("count", 3), low=1, high=10)
        orientation = str(args.get("orientation", "")).strip().lower()
        if orientation not in {"landscape", "portrait", "squarish"}:
            orientation = ""

        params: dict[str, Any] = {
            "query": query,
            "per_page": count,
            "content_filter": "high",
        }
        if orientation:
            params["orientation"] = orientation

        headers = {
            "Authorization": f"Client-ID {access_key}",
            "Accept-Version": "v1",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                "https://api.unsplash.com/search/photos",
                params=params,
                headers=headers,
            )
        if response.status_code != 200:
            log_tool_run(
                getattr(context, "tasks_dir", Path.cwd()),
                "search_photos",
                args=args,
                error=f"Unsplash API error: {response.status_code} {response.text}",
                duration_ms=(time.monotonic() - start) * 1000.0,
            )
            return _error(f"Unsplash API error: {response.status_code} {response.text}")

        payload = response.json()
        results = []
        for item in payload.get("results", []):
            urls = item.get("urls") or {}
            url = urls.get("regular") or urls.get("full") or urls.get("raw")
            if not url:
                continue
            url = _append_tracking(url, context.app_name)
            user = item.get("user") or {}
            results.append(
                {
                    "id": item.get("id"),
                    "url": url,
                    "alt": item.get("alt_description")
                    or item.get("description")
                    or query,
                    "photographer": user.get("name"),
                    "profile": (user.get("links") or {}).get("html"),
                }
            )

        log_tool_run(
            getattr(context, "tasks_dir", Path.cwd()),
            "search_photos",
            args=args,
            result={"ok": True, "count": len(results), "query": query},
            duration_ms=(time.monotonic() - start) * 1000.0,
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"query": query, "results": results}, ensure_ascii=True, indent=2
                    ),
                }
            ]
        }

    return create_sdk_mcp_server(
        name=UNSPLASH_SERVER_NAME,
        version="1.0.0",
        tools=[search_photos],
    )


def _append_tracking(url: str, app_name: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("utm_source", app_name)
    query.setdefault("utm_medium", "referral")
    new_query = urlencode(query)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _clamp_int(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def _error(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }
