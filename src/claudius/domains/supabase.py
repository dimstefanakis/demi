from __future__ import annotations

import asyncio
from dataclasses import dataclass
import secrets
import string
from typing import Any

import httpx

from claudius.config import Settings


class SupabaseError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SupabaseProject:
    ref: str
    project_id: str | None
    name: str | None
    status: str | None
    region: str | None
    api_url: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class SupabaseKeys:
    publishable_key: str | None
    secret_key: str | None
    anon_key: str | None
    service_role_key: str | None
    raw: list[dict[str, Any]]


class SupabaseClient:
    def __init__(self, settings: Settings):
        if not settings.supabase_access_token:
            raise ValueError("SUPABASE_ACCESS_TOKEN is required")
        self.settings = settings
        self.base_url = settings.supabase_api_base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {settings.supabase_access_token}",
            "Content-Type": "application/json",
        }

    async def create_project(
        self,
        name: str,
        *,
        region_selection: str | None = None,
        region: str | None = None,
        instance_size: str | None = None,
        db_password: str | None = None,
    ) -> SupabaseProject:
        payload: dict[str, Any] = {
            "name": name,
            "db_pass": db_password or _generate_password(),
        }
        if self.settings.supabase_org_slug:
            payload["organization_slug"] = self.settings.supabase_org_slug
        elif self.settings.supabase_org_id:
            payload["organization_id"] = self.settings.supabase_org_id
        selection = region_selection or self.settings.supabase_region_selection
        if selection:
            payload["region_selection"] = {
                "type": "smartGroup",
                "code": selection,
            }
        elif region or self.settings.supabase_region:
            payload["region"] = region or self.settings.supabase_region
        size = instance_size or self.settings.supabase_instance_size
        if size:
            payload["desired_instance_size"] = size

        data = await self._request_json("POST", "/v1/projects", json=payload)
        ref = str(data.get("ref") or data.get("project_ref") or data.get("id") or "").strip()
        if not ref:
            raise SupabaseError(500, f"Unexpected Supabase response: {data}")
        api_url = f"https://{ref}.supabase.co"
        return SupabaseProject(
            ref=ref,
            project_id=_string_or_none(data.get("id")),
            name=_string_or_none(data.get("name")),
            status=_string_or_none(data.get("status")),
            region=_string_or_none(data.get("region")),
            api_url=api_url,
            raw=data,
        )

    async def get_project(self, ref: str) -> dict[str, Any] | None:
        try:
            return await self._request_json("GET", f"/v1/projects/{ref}")
        except SupabaseError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def list_billing_addons(self, ref: str) -> dict[str, Any]:
        return await self._request_json("GET", f"/v1/projects/{ref}/billing/addons")

    async def apply_compute_addon(self, ref: str, addon_variant: str) -> Any:
        payload = {"addon_type": "compute_instance", "addon_variant": addon_variant}
        return await self._request_json(
            "PATCH", f"/v1/projects/{ref}/billing/addons", json=payload
        )

    async def list_projects(self) -> list[dict[str, Any]]:
        data = await self._request_json("GET", "/v1/projects")
        if isinstance(data, list):
            return data
        return []

    async def get_project_status(self, ref: str) -> str | None:
        data = await self.get_project(ref)
        if data:
            return _string_or_none(data.get("status"))
        for project in await self.list_projects():
            if str(project.get("ref") or "").strip() == ref:
                return _string_or_none(project.get("status"))
        return None

    async def wait_for_project_ready(
        self, ref: str, timeout_seconds: int = 900, interval_seconds: int = 10
    ) -> str | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        last_status = None
        while loop.time() < deadline:
            try:
                status = await self.get_project_status(ref)
            except SupabaseError:
                status = None
            if status:
                last_status = status
            if status and status.upper() == "ACTIVE_HEALTHY":
                return status
            await asyncio.sleep(interval_seconds)
        return last_status

    async def get_api_keys(self, ref: str) -> SupabaseKeys:
        data = await self._request_json("GET", f"/v1/projects/{ref}/api-keys", params={"reveal": "true"})
        if not isinstance(data, list):
            data = []
        publishable = _extract_key(data, {"publishable"})
        secret = _extract_key(data, {"secret"})
        anon = _extract_key(data, {"anon", "anonymous"})
        service_role = _extract_key(data, {"service_role", "service-role", "service"})
        return SupabaseKeys(
            publishable_key=publishable,
            secret_key=secret,
            anon_key=anon,
            service_role_key=service_role,
            raw=data,
        )

    async def ensure_api_keys(self, ref: str) -> SupabaseKeys:
        keys = await self.get_api_keys(ref)
        if keys.publishable_key and keys.secret_key:
            return keys

        if not keys.publishable_key:
            await self._create_api_key(ref, name="publishable", key_type="publishable")
        if not keys.secret_key:
            await self._create_api_key(ref, name="secret", key_type="secret")
        return await self.get_api_keys(ref)

    async def wait_for_api_keys(
        self, ref: str, timeout_seconds: int = 900, interval_seconds: int = 10
    ) -> SupabaseKeys:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        last_error: Exception | None = None
        while loop.time() < deadline:
            try:
                return await self.ensure_api_keys(ref)
            except SupabaseError as exc:
                last_error = exc
                if exc.status_code not in {400, 404, 409, 423}:
                    raise
            await asyncio.sleep(interval_seconds)
        raise SupabaseError(500, f"Timed out waiting for Supabase API keys: {last_error}")

    async def _create_api_key(self, ref: str, name: str, key_type: str) -> None:
        payload = {"name": name, "type": key_type}
        try:
            await self._request_json("POST", f"/v1/projects/{ref}/api-keys", json=payload)
        except SupabaseError as exc:
            if exc.status_code in {409, 422}:
                return
            raise

    async def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.request(method, url, headers=self.headers, **kwargs)
        if response.status_code >= 400:
            raise SupabaseError(response.status_code, response.text)
        try:
            return response.json()
        except ValueError:
            raise SupabaseError(response.status_code, response.text)


def _generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _extract_key(items: list[dict[str, Any]], names: set[str]) -> str | None:
    for item in items:
        key_name = str(item.get("name") or item.get("type") or "").lower()
        if key_name in names:
            return _string_or_none(item.get("api_key") or item.get("key") or item.get("value"))
    return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
