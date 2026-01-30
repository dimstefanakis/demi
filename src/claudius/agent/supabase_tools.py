from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import secrets
import string
import time
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool

from claudius.agent.tool_logging import log_tool_run
from claudius.config import Settings
from claudius.db.core import Database
from claudius.domains.supabase import SupabaseClient, SupabaseError
from claudius.tenant_db import ensure_tenant_db
from claudius.domains.vercel_env import set_env_vars


SUPABASE_SERVER_NAME = "claudius-supabase"


@dataclass(frozen=True)
class SupabaseToolContext:
    tasks_dir: Path
    db: Database | None = None
    tenant_id: int | None = None


def build_supabase_tools(context: SupabaseToolContext) -> list[SdkMcpTool[Any]]:
    def _log(
        tool_name: str,
        args: dict[str, Any],
        result: Any | None = None,
        error: str | None = None,
        start: float | None = None,
    ) -> None:
        duration_ms = None
        if start is not None:
            duration_ms = (time.monotonic() - start) * 1000.0
        log_tool_run(
            context.tasks_dir,
            tool_name,
            args=args,
            result=result,
            error=error,
            duration_ms=duration_ms,
        )

    @tool(
        "provision_managed_backend",
        "Provision a managed backend instance and inject env vars into the tenant app.",
        {
            "type": "object",
            "properties": {
                "order_id": {"type": "number"},
                "project_name": {"type": "string"},
                "region_selection": {"type": "string"},
                "region": {"type": "string"},
                "instance_size": {"type": "string"},
            },
            "required": [],
        },
    )
    async def provision_managed_backend(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        has_db = context.db is not None and context.tenant_id is not None

        order_id = args.get("order_id")
        project_name = str(args.get("project_name") or "").strip()
        region_selection = str(args.get("region_selection") or "").strip()
        region = str(args.get("region") or "").strip()
        instance_size = str(args.get("instance_size") or "").strip()
        cached_state = _load_project_state(context.tasks_dir)
        db_password = _extract_db_password(cached_state)

        if not region_selection and not region:
            payload = {"ok": False, "status": "region_required"}
            _log("provision_managed_backend", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        if instance_size.lower() == "nano":
            payload = {"ok": False, "status": "instance_not_allowed"}
            _log("provision_managed_backend", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        if order_id is not None and has_db:
            try:
                context.db.update_billing_order_status(int(order_id), "provisioning")
            except (TypeError, ValueError):
                order_id = None

        settings = Settings()
        if not settings.supabase_access_token:
            payload = {"ok": False, "status": "missing_token"}
            _log("provision_managed_backend", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }
        try:
            client = SupabaseClient(settings)
        except ValueError as exc:
            if order_id is not None and has_db:
                context.db.update_billing_order_status(int(order_id), "failed", error=str(exc))
            payload = {"ok": False, "status": "missing_token", "error": str(exc)}
            _log("provision_managed_backend", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        project_ref = None
        if has_db:
            existing = context.db.get_supabase_project(context.tenant_id)
            project_ref = (
                str(existing["project_ref"]).strip() if existing and existing["project_ref"] else None
            )
        elif cached_state:
            project_ref = str(cached_state.get("project_ref") or "").strip() or None

        project: Any | None = None
        if project_ref:
            project = await client.get_project(project_ref) or {"ref": project_ref}
        else:
            if not settings.supabase_org_slug and not settings.supabase_org_id:
                payload = {"ok": False, "status": "missing_org"}
                _log("provision_managed_backend", args, result=payload, start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
            if not project_name:
                project_name = _safe_project_name(
                    settings.supabase_project_prefix,
                    context.tasks_dir,
                    context.tenant_id,
                )
            if not db_password:
                db_password = _generate_db_password()
            try:
                project = await client.create_project(
                    project_name,
                    region_selection=region_selection or None,
                    region=region or None,
                    instance_size=instance_size or None,
                    db_password=db_password,
                )
            except SupabaseError as exc:
                if order_id is not None and has_db:
                    context.db.update_billing_order_status(int(order_id), "failed", error=str(exc))
                payload = {"ok": False, "status": "create_failed", "error": str(exc)}
                _log("provision_managed_backend", args, result=payload, error=str(exc), start=start)
                return {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                    "is_error": True,
                }
            project_ref = project.ref

        status = await client.wait_for_project_ready(project_ref)
        try:
            keys = await client.wait_for_api_keys(project_ref)
        except SupabaseError as exc:
            if order_id is not None and has_db:
                context.db.update_billing_order_status(int(order_id), "failed", error=str(exc))
            payload = {"ok": False, "status": "keys_failed", "error": str(exc)}
            _log("provision_managed_backend", args, result=payload, error=str(exc), start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        public_key = keys.publishable_key or keys.anon_key
        if not public_key:
            if order_id is not None and has_db:
                context.db.update_billing_order_status(
                    int(order_id), "failed", error="missing_publishable_key"
                )
            payload = {"ok": False, "status": "missing_key"}
            _log("provision_managed_backend", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        api_url = f"https://{project_ref}.supabase.co"
        project_id = None
        project_name_out = None
        region_out = None
        raw_project = None
        if isinstance(project, dict):
            raw_project = project
            project_id = _string_or_none(project.get("id"))
            project_name_out = _string_or_none(project.get("name"))
            region_out = _string_or_none(project.get("region"))
        elif project is not None:
            raw_project = project.raw
            project_id = project.project_id
            project_name_out = project.name
            region_out = project.region

        if has_db:
            context.db.upsert_supabase_project(
                tenant_id=context.tenant_id,
                project_ref=project_ref,
                project_id=project_id,
                project_name=project_name_out,
                region=region_out or region or region_selection or None,
                status=status,
                api_url=api_url,
                publishable_key=keys.publishable_key,
                secret_key=keys.secret_key,
                anon_key=keys.anon_key,
                service_role_key=keys.service_role_key,
                raw={"project": raw_project, "keys": keys.raw},
            )
        project_state = {
            "project_ref": project_ref,
            "project_id": project_id,
            "project_name": project_name_out,
            "region": region_out or region or region_selection or None,
            "status": status,
            "api_url": api_url,
            "publishable_key": keys.publishable_key,
            "secret_key": keys.secret_key,
            "anon_key": keys.anon_key,
            "service_role_key": keys.service_role_key,
        }
        if db_password:
            project_state["db_password"] = db_password
        _persist_project_state(context.tasks_dir, project_state)

        project_dir = _resolve_app_dir(context.tasks_dir)
        if project_dir is None:
            if order_id is not None and has_db:
                context.db.update_billing_order_status(
                    int(order_id), "provisioned", error="vercel_project_not_found"
                )
            payload = {
                "ok": True,
                "status": "provisioned_no_env",
                "project_ref": project_ref,
                "api_url": api_url,
            }
            _log("provision_managed_backend", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }

        env_values = {
            "NEXT_PUBLIC_SUPABASE_URL": api_url,
            "NEXT_PUBLIC_SUPABASE_ANON_KEY": public_key,
            "SUPABASE_URL": api_url,
        }
        if keys.secret_key or keys.service_role_key:
            env_values["SUPABASE_SECRET_KEY"] = keys.secret_key or keys.service_role_key or ""

        results = set_env_vars(project_dir, env_values, settings)
        failed = [result for result in results if not result.success]
        if failed and order_id is not None and has_db:
            context.db.update_billing_order_status(
                int(order_id),
                "env_failed",
                error="; ".join(result.output for result in failed if result.output),
            )
            payload = {
                "ok": True,
                "status": "env_failed",
                "project_ref": project_ref,
                "api_url": api_url,
                "env_results": _summarize_env_results(results),
            }
            _log("provision_managed_backend", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": False,
            }

        if order_id is not None and has_db:
            context.db.update_billing_order_status(int(order_id), "provisioned")

        payload = {
            "ok": True,
            "status": "provisioned",
            "project_ref": project_ref,
            "api_url": api_url,
            "env_results": _summarize_env_results(results),
        }
        _log("provision_managed_backend", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    @tool(
        "upgrade_managed_backend",
        "Upgrade an existing managed backend instance to a new size.",
        {
            "type": "object",
            "properties": {
                "order_id": {"type": "number"},
                "instance_size": {"type": "string"},
            },
            "required": ["instance_size"],
        },
    )
    async def upgrade_managed_backend(args: dict[str, Any]) -> dict[str, Any]:
        start = time.monotonic()
        has_db = context.db is not None and context.tenant_id is not None

        settings = Settings()
        if not settings.supabase_access_token:
            payload = {"ok": False, "status": "missing_token"}
            _log("upgrade_managed_backend", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        order_id = args.get("order_id")
        instance_size = str(args.get("instance_size") or "").strip().lower()
        if not instance_size or instance_size == "nano":
            payload = {"ok": False, "status": "invalid_instance"}
            _log("upgrade_managed_backend", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        project_ref = None
        if has_db:
            existing = context.db.get_supabase_project(context.tenant_id)
            project_ref = (
                str(existing["project_ref"]).strip() if existing and existing["project_ref"] else None
            )
        else:
            cached = _load_project_state(context.tasks_dir)
            if cached:
                project_ref = str(cached.get("project_ref") or "").strip() or None
        if not project_ref:
            payload = {"ok": False, "status": "missing_project"}
            _log("upgrade_managed_backend", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        try:
            client = SupabaseClient(settings)
        except ValueError as exc:
            payload = {"ok": False, "status": "missing_token", "error": str(exc)}
            _log("upgrade_managed_backend", args, result=payload, start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        if order_id is not None and has_db:
            try:
                context.db.update_billing_order_status(int(order_id), "upgrading")
            except (TypeError, ValueError):
                order_id = None

        addon_variant = instance_size
        try:
            addons = await client.list_billing_addons(project_ref)
        except SupabaseError as exc:
            addons = {"error": str(exc)}
        variant = _select_compute_variant(addons, instance_size)
        if variant:
            addon_variant = variant

        try:
            await client.apply_compute_addon(project_ref, addon_variant=addon_variant)
        except SupabaseError as exc:
            if order_id is not None and has_db:
                context.db.update_billing_order_status(int(order_id), "failed", error=str(exc))
            payload = {"ok": False, "status": "upgrade_failed", "error": str(exc)}
            _log("upgrade_managed_backend", args, result=payload, error=str(exc), start=start)
            return {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "is_error": True,
            }

        if order_id is not None and has_db:
            context.db.update_billing_order_status(int(order_id), "upgraded")

        payload = {
            "ok": True,
            "status": "upgraded",
            "project_ref": project_ref,
            "instance_size": instance_size,
        }
        _log("upgrade_managed_backend", args, result=payload, start=start)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "is_error": False,
        }

    return [provision_managed_backend, upgrade_managed_backend]


def build_supabase_server(context: SupabaseToolContext) -> McpSdkServerConfig:
    return create_sdk_mcp_server(
        name=SUPABASE_SERVER_NAME,
        version="1.0.0",
        tools=build_supabase_tools(context),
    )


def _resolve_app_dir(tasks_dir: Path) -> Path | None:
    workspace_root = tasks_dir.parent
    site_dir = workspace_root / "site"
    app_name_path = tasks_dir / "app_name.txt"
    if app_name_path.exists():
        name = app_name_path.read_text().strip()
        if name:
            candidate = site_dir / name
            if (candidate / "package.json").exists():
                return candidate
    if (site_dir / "package.json").exists():
        return site_dir
    for child in site_dir.iterdir():
        if child.is_dir() and (child / "package.json").exists():
            return child
    return None


def _safe_project_name(prefix: str, tasks_dir: Path, tenant_id: int | None) -> str:
    if tenant_id is not None:
        return f"{prefix}-{tenant_id}"
    return f"{prefix}-{tasks_dir.parent.name}"


def _extract_db_password(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    value = payload.get("db_password")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _tenant_db(tasks_dir: Path):
    return ensure_tenant_db(tasks_dir.parent / "tenant.sqlite")


def _load_project_file(tasks_dir: Path) -> dict[str, Any] | None:
    path = tasks_dir / "supabase_project.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _persist_project_file(tasks_dir: Path, payload: dict[str, Any]) -> None:
    try:
        (tasks_dir / "supabase_project.json").write_text(json.dumps(payload, indent=2))
    except OSError:
        return


def _load_project_state(tasks_dir: Path) -> dict[str, Any] | None:
    db = _tenant_db(tasks_dir)
    payload = db.get_kv("supabase", "project")
    if payload:
        return payload
    return _load_project_file(tasks_dir)


def _persist_project_state(tasks_dir: Path, payload: dict[str, Any]) -> None:
    db = _tenant_db(tasks_dir)
    db.set_kv("supabase", "project", payload)
    _persist_project_file(tasks_dir, payload)

def _generate_db_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _summarize_env_results(results: list[Any]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for result in results:
        summary.append({"name": result.name, "success": result.success})
    return summary


def _select_compute_variant(addons: Any, instance_size: str) -> str | None:
    items: list[dict[str, Any]] = []
    if isinstance(addons, list):
        items = addons
    elif isinstance(addons, dict):
        for key in ("addons", "data", "items"):
            if isinstance(addons.get(key), list):
                items = addons[key]
                break
        if not items and isinstance(addons.get("compute_instance"), dict):
            compute = addons.get("compute_instance") or {}
            for key in ("variants", "items", "data"):
                if isinstance(compute.get(key), list):
                    items = compute[key]
                    break
    if not items:
        return None
    size = instance_size.lower()
    for item in items:
        addon_type = str(item.get("addon_type") or item.get("type") or "").lower()
        if addon_type and "compute" not in addon_type:
            continue
        variant = str(item.get("addon_variant") or item.get("variant") or "").lower()
        name = str(item.get("name") or item.get("plan") or item.get("product") or "").lower()
        if variant == size:
            return variant
        if size and (size in variant or size in name):
            return item.get("addon_variant") or item.get("variant") or variant
    return None
