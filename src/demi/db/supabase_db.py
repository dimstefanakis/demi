from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import json
from typing import Any
import uuid

from supabase import Client, create_client
from postgrest.exceptions import APIError

from demi.models import NormalizedMessage, Tenant

DEFAULT_EXECUTION_CONTEXT = "Main project"
DEFAULT_EXECUTION_AGENT_ROLE = "execution"
DEFAULT_RUN_ROLE = "execution"
VALID_EXECUTION_AGENT_ROLES = {
    "execution",
    "project_manager",
    "lead_project_manager",
}
VALID_RUN_ROLES = set(VALID_EXECUTION_AGENT_ROLES)


@dataclass
class SupabaseDatabase:
    url: str
    service_key: str
    client: Client | None = None
    _execution_stream_fallback: dict[int, list[dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.url or not self.service_key:
            raise ValueError("Supabase URL and service key are required for main DB")
        self.client = create_client(self.url, self.service_key)

    def init(self) -> None:
        # Basic connectivity check; will raise if schema is missing.
        self._execute(self._table("tenants").select("id").limit(1))

    def _table(self, name: str):
        if self.client is None:
            raise RuntimeError("Supabase client not initialized")
        return self.client.table(name)

    def _execute(self, query):
        response = query.execute()
        error = getattr(response, "error", None)
        if error:
            raise RuntimeError(str(error))
        return getattr(response, "data", None)

    @staticmethod
    def _is_unique_violation_error(exc: Exception) -> bool:
        if isinstance(exc, APIError):
            try:
                if exc.code == "23505":
                    return True
            except Exception:
                pass
        message = str(exc).lower()
        if "23505" in message:
            return True
        if "duplicate key value" in message:
            return True
        if "violates unique constraint" in message:
            return True
        if "unique constraint" in message:
            return True
        return False

    @staticmethod
    def _is_schema_cache_missing(exc: Exception, token: str | None = None) -> bool:
        lowered = str(exc).lower()
        if isinstance(exc, APIError):
            try:
                if exc.code == "PGRST204":
                    if token:
                        return token.lower() in lowered
                    return True
            except Exception:
                pass
        if "schema cache" not in lowered:
            return False
        if token:
            return token.lower() in lowered
        return True

    @staticmethod
    def _is_missing_column(exc: Exception, column: str | None = None) -> bool:
        if isinstance(exc, APIError):
            try:
                if exc.code == "42703":
                    if not column:
                        return True
            except Exception:
                pass
        lowered = str(exc).lower()
        if "does not exist" not in lowered or "column" not in lowered:
            return False
        if not column:
            return True
        needle = str(column or "").strip().lower()
        if not needle:
            return True
        if f"column {needle} does not exist" in lowered:
            return True
        if f'column "{needle}" does not exist' in lowered:
            return True
        short = needle.split(".")[-1]
        if f"column {short} does not exist" in lowered:
            return True
        if f'column "{short}" does not exist' in lowered:
            return True
        if f".{short} does not exist" in lowered:
            return True
        return False

    @staticmethod
    def _is_missing_relation(exc: Exception, relation: str) -> bool:
        if isinstance(exc, APIError):
            try:
                if exc.code == "42P01":
                    return True
            except Exception:
                pass
        lowered = str(exc).lower()
        rel = str(relation or "").strip().lower()
        if not rel:
            return False
        if f"relation '{rel}' does not exist" in lowered:
            return True
        if f'relation "{rel}" does not exist' in lowered:
            return True
        if f"table '{rel}'" in lowered and "schema cache" in lowered:
            return True
        return False

    @staticmethod
    def _normalize_execution_agent_role(role: Any | None) -> str:
        value = str(role or "").strip().lower()
        if value in VALID_EXECUTION_AGENT_ROLES:
            return value
        return DEFAULT_EXECUTION_AGENT_ROLE

    @staticmethod
    def _normalize_run_role(role: Any | None) -> str:
        value = str(role or "").strip().lower()
        if value in VALID_RUN_ROLES:
            return value
        return DEFAULT_RUN_ROLE

    @staticmethod
    def _legacy_execution_agent_row(
        tenant_id: int,
        context: str | None = None,
        role: Any | None = None,
    ) -> dict[str, Any]:
        normalized_role = SupabaseDatabase._normalize_execution_agent_role(role)
        return {
            "id": None,
            "tenant_id": int(tenant_id),
            "context": str(context or "").strip() or DEFAULT_EXECUTION_CONTEXT,
            "role": normalized_role,
            "status": "active",
            "session_id": None,
            "last_run_id": None,
            "metadata_json": None,
            "created_at": None,
            "updated_at": None,
            "archived_at": None,
        }

    def _read_default_execution_session_from_kv(self, tenant_id: int) -> str | None:
        for key in ("claude_session", "claude_session:main"):
            try:
                payload = self.get_tenant_kv(int(tenant_id), "execution", key) or {}
            except Exception:
                payload = {}
            value = str(payload.get("session_id") or "").strip()
            if value:
                return value
        return None

    def _select_one(self, table: str, **filters: Any) -> dict[str, Any] | None:
        query = self._table(table).select("*")
        for key, value in filters.items():
            query = query.eq(key, value)
        data = self._execute(query.limit(1))
        if data:
            return data[0]
        return None

    def _row_to_tenant(self, row: dict[str, Any]) -> Tenant:
        return Tenant(
            id=int(row["id"]),
            provider=row["provider"],
            external_id=row["external_id"],
            key=row["key"],
            workspace_path=row.get("workspace_path"),
            session_id=row.get("session_id"),
            vercel_project_id=row.get("vercel_project_id"),
            last_deploy_url=row.get("last_deploy_url"),
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
        )

    def get_or_create_tenant(self, provider: str, external_id: str) -> Tenant:
        key = f"{provider}:{external_id}"
        row = self._select_one("tenants", key=key)
        if row:
            return self._row_to_tenant(row)

        now = datetime.now(tz=timezone.utc).isoformat()
        payload = {
            "provider": provider,
            "external_id": external_id,
            "key": key,
            "workspace_path": None,
            "session_id": None,
            "vercel_project_id": None,
            "last_deploy_url": None,
            "created_at": now,
            "updated_at": now,
        }
        self._execute(self._table("tenants").insert(payload))
        row = self._select_one("tenants", key=key)
        if not row:
            raise RuntimeError("tenant_create_failed")
        return self._row_to_tenant(row)

    def get_tenant_by_id(self, tenant_id: int) -> Tenant | None:
        row = self._select_one("tenants", id=tenant_id)
        return self._row_to_tenant(row) if row else None

    def get_tenant_by_key(self, key: str) -> Tenant | None:
        row = self._select_one("tenants", key=key)
        return self._row_to_tenant(row) if row else None

    def get_tenant_by_external(self, provider: str, external_id: str) -> Tenant | None:
        row = self._select_one("tenants", provider=provider, external_id=external_id)
        return self._row_to_tenant(row) if row else None

    def list_tenants(self, page_size: int = 1000) -> list[Tenant]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            batch = self._execute(
                self._table("tenants")
                .select("*")
                .order("id", desc=False)
                .range(offset, offset + page_size - 1)
            )
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size
        return [self._row_to_tenant(row) for row in rows]

    def update_tenant_workspace(self, tenant_id: int, workspace_path: str) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._execute(
            self._table("tenants")
            .update({"workspace_path": workspace_path, "updated_at": now})
            .eq("id", tenant_id)
        )

    def update_tenant_session(self, tenant_id: int, session_id: str) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._execute(
            self._table("tenants")
            .update({"session_id": session_id, "updated_at": now})
            .eq("id", tenant_id)
        )

    def update_tenant_deploy_url(self, tenant_id: int, url: str) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._execute(
            self._table("tenants")
            .update({"last_deploy_url": url, "updated_at": now})
            .eq("id", tenant_id)
        )

    @staticmethod
    def _normalize_execution_context(context: str | None) -> str:
        value = str(context or "").strip()
        return value or DEFAULT_EXECUTION_CONTEXT

    def list_execution_agents(
        self,
        tenant_id: int,
        *,
        include_inactive: bool = False,
        limit: int = 50,
        role: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_role = (
            self._normalize_execution_agent_role(role)
            if role is not None
            else None
        )
        try:
            query = (
                self._table("execution_agents")
                .select("*")
                .eq("tenant_id", int(tenant_id))
                .order("updated_at", desc=True)
                .limit(max(1, int(limit)))
            )
            if normalized_role is not None:
                query = query.eq("role", normalized_role)
            if not include_inactive:
                query = query.eq("status", "active")
            data = self._execute(query)
            rows = [dict(row) for row in list(data or [])]
            for row in rows:
                row.setdefault("role", DEFAULT_EXECUTION_AGENT_ROLE)
            return rows
        except Exception as exc:
            if normalized_role is not None and (
                self._is_schema_cache_missing(exc, "role")
                or self._is_missing_column(exc, "execution_agents.role")
            ):
                if normalized_role != DEFAULT_EXECUTION_AGENT_ROLE:
                    return []
                return self.list_execution_agents(
                    int(tenant_id),
                    include_inactive=include_inactive,
                    limit=limit,
                    role=None,
                )
            if self._is_missing_relation(exc, "execution_agents"):
                return []
            raise

    def get_execution_agent(self, execution_agent_id: int) -> dict[str, Any] | None:
        try:
            row = self._select_one("execution_agents", id=int(execution_agent_id))
        except Exception as exc:
            if self._is_missing_relation(exc, "execution_agents"):
                return None
            raise
        if isinstance(row, dict) and "role" not in row:
            row = dict(row)
            row["role"] = DEFAULT_EXECUTION_AGENT_ROLE
        return row

    def get_execution_agent_by_context(
        self,
        tenant_id: int,
        context: str | None,
        role: str = DEFAULT_EXECUTION_AGENT_ROLE,
    ) -> dict[str, Any] | None:
        normalized = self._normalize_execution_context(context)
        normalized_role = self._normalize_execution_agent_role(role)
        try:
            data = self._execute(
                self._table("execution_agents")
                .select("*")
                .eq("tenant_id", int(tenant_id))
                .eq("context", normalized)
                .eq("role", normalized_role)
                .order("updated_at", desc=True)
                .limit(1)
            )
        except Exception as exc:
            if self._is_schema_cache_missing(exc, "role") or self._is_missing_column(
                exc,
                "execution_agents.role",
            ):
                if normalized_role != DEFAULT_EXECUTION_AGENT_ROLE:
                    return None
                try:
                    data = self._execute(
                        self._table("execution_agents")
                        .select("*")
                        .eq("tenant_id", int(tenant_id))
                        .eq("context", normalized)
                        .order("updated_at", desc=True)
                        .limit(1)
                    )
                except Exception as fallback_exc:
                    if self._is_missing_relation(fallback_exc, "execution_agents"):
                        return None
                    raise
            elif self._is_missing_relation(exc, "execution_agents"):
                return None
            else:
                raise
        if data:
            row = dict(data[0])
            if "role" not in row:
                row["role"] = DEFAULT_EXECUTION_AGENT_ROLE
            return row
        return None

    def create_execution_agent(
        self,
        *,
        tenant_id: int,
        context: str | None,
        role: str = DEFAULT_EXECUTION_AGENT_ROLE,
        session_id: str | None = None,
        status: str = "active",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_role = self._normalize_execution_agent_role(role)
        now = datetime.now(tz=timezone.utc).isoformat()
        payload = {
            "tenant_id": int(tenant_id),
            "context": self._normalize_execution_context(context),
            "role": normalized_role,
            "status": str(status or "active").strip() or "active",
            "session_id": str(session_id or "").strip() or None,
            "metadata_json": metadata,
            "last_run_id": None,
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
        }
        try:
            data = self._execute(self._table("execution_agents").insert(payload))
        except Exception as exc:
            if self._is_schema_cache_missing(exc, "role") or self._is_missing_column(
                exc,
                "execution_agents.role",
            ):
                if normalized_role != DEFAULT_EXECUTION_AGENT_ROLE:
                    raise RuntimeError("execution_agent_role_column_missing") from exc
                legacy_payload = dict(payload)
                legacy_payload.pop("role", None)
                data = self._execute(self._table("execution_agents").insert(legacy_payload))
            elif self._is_missing_relation(exc, "execution_agents"):
                return self._legacy_execution_agent_row(
                    int(tenant_id),
                    payload["context"],
                    role=normalized_role,
                )
            else:
                raise
        if not data:
            raise RuntimeError("execution_agent_create_failed")
        row = dict(data[0])
        if "role" not in row:
            row["role"] = normalized_role
        return row

    def ensure_execution_agent(
        self,
        *,
        tenant_id: int,
        context: str | None = None,
        role: str = DEFAULT_EXECUTION_AGENT_ROLE,
    ) -> dict[str, Any]:
        normalized = self._normalize_execution_context(context)
        normalized_role = self._normalize_execution_agent_role(role)
        fallback_session_id: str | None = None
        if (
            normalized == DEFAULT_EXECUTION_CONTEXT
            and normalized_role == DEFAULT_EXECUTION_AGENT_ROLE
        ):
            fallback_session_id = self._read_default_execution_session_from_kv(int(tenant_id))
        existing = self.get_execution_agent_by_context(
            int(tenant_id),
            normalized,
            role=normalized_role,
        )
        if existing:
            status = str(existing.get("status") or "").strip().lower()
            archived_at = existing.get("archived_at")
            if status != "active" or archived_at:
                revived = self.update_execution_agent(
                    int(existing["id"]),
                    status="active",
                    archived=False,
                )
                if revived:
                    return revived
            existing_session = str(existing.get("session_id") or "").strip()
            if not existing_session and fallback_session_id:
                try:
                    hydrated = self.update_execution_agent(
                        int(existing["id"]),
                        session_id=fallback_session_id,
                        status="active",
                        archived=False,
                    )
                except Exception:
                    hydrated = None
                if isinstance(hydrated, dict):
                    return hydrated
            return existing
        try:
            return self.create_execution_agent(
                tenant_id=int(tenant_id),
                context=normalized,
                role=normalized_role,
                session_id=fallback_session_id,
                status="active",
            )
        except Exception as exc:
            if self._is_missing_relation(exc, "execution_agents"):
                return self._legacy_execution_agent_row(
                    int(tenant_id),
                    normalized,
                    role=normalized_role,
                )
            existing = self.get_execution_agent_by_context(
                int(tenant_id),
                normalized,
                role=normalized_role,
            )
            if existing:
                return existing
            raise

    def update_execution_agent(
        self,
        execution_agent_id: int,
        *,
        context: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        last_run_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        archived: bool | None = None,
    ) -> dict[str, Any] | None:
        updates: dict[str, Any] = {
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        if context is not None:
            updates["context"] = self._normalize_execution_context(context)
        if session_id is not None:
            updates["session_id"] = str(session_id or "").strip() or None
        if status is not None:
            updates["status"] = str(status).strip() or "active"
        if last_run_id is not None:
            updates["last_run_id"] = int(last_run_id)
        if metadata is not None:
            updates["metadata_json"] = metadata
        if archived is True:
            updates["archived_at"] = datetime.now(tz=timezone.utc).isoformat()
            if "status" not in updates:
                updates["status"] = "archived"
        elif archived is False:
            updates["archived_at"] = None
            if "status" not in updates:
                updates["status"] = "active"
        try:
            self._execute(
                self._table("execution_agents")
                .update(updates)
                .eq("id", int(execution_agent_id))
            )
        except Exception as exc:
            if self._is_missing_relation(exc, "execution_agents"):
                return None
            raise
        return self.get_execution_agent(int(execution_agent_id))

    def create_event_job(
        self,
        tenant_id: int,
        job_type: str,
        payload: dict[str, Any],
        run_after: str | None = None,
    ) -> int:
        now = datetime.now(tz=timezone.utc).isoformat()
        payload_row = {
            "tenant_id": tenant_id,
            "job_type": job_type,
            "payload_json": payload,
            "status": "pending",
            "attempts": 0,
            "run_after": run_after or now,
            "last_error": None,
            "created_at": now,
            "updated_at": now,
        }
        data = self._execute(self._table("event_jobs").insert(payload_row))
        if not data:
            raise RuntimeError("event_job_create_failed")
        return int(data[0]["id"])

    def fetch_pending_event_jobs(
        self,
        limit: int = 25,
        job_type_filter: str | list[str] | None = None,
    ) -> list[dict[str, Any]]:
        now = datetime.now(tz=timezone.utc).isoformat()
        query = (
            self._table("event_jobs")
            .select("*")
            .eq("status", "pending")
            .lte("run_after", now)
            .order("id", desc=False)
            .limit(limit)
        )
        if isinstance(job_type_filter, str):
            job_type = job_type_filter.strip()
            if job_type:
                query = query.eq("job_type", job_type)
        elif isinstance(job_type_filter, list):
            values = [str(item).strip() for item in job_type_filter if str(item).strip()]
            if values:
                query = query.in_("job_type", values)
        data = self._execute(query)
        return list(data or [])

    def list_event_jobs(
        self,
        tenant_id: int | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = self._table("event_jobs").select("*").order("id", desc=False)
        if tenant_id is not None:
            query = query.eq("tenant_id", tenant_id)
        if status:
            query = query.eq("status", status)
        if limit:
            query = query.limit(limit)
        data = self._execute(query)
        return list(data or [])

    def mark_event_job_running(self, job_id: int) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._execute(
            self._table("event_jobs")
            .update({"status": "running", "updated_at": now})
            .eq("id", job_id)
        )

    def mark_event_job_done(self, job_id: int) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._execute(
            self._table("event_jobs")
            .update({"status": "completed", "updated_at": now})
            .eq("id", job_id)
        )

    def mark_event_job_failed(
        self,
        job_id: int,
        error: str,
        retry_after_seconds: int | None = None,
        max_attempts: int = 5,
    ) -> None:
        row = self._select_one("event_jobs", id=job_id)
        attempts = int(row.get("attempts") or 0) + 1 if row else 1
        now = datetime.now(tz=timezone.utc)
        status = "failed" if attempts >= max_attempts else "pending"
        run_after = now
        if retry_after_seconds and status == "pending":
            run_after = now + timedelta(seconds=retry_after_seconds)
        self._execute(
            self._table("event_jobs")
            .update(
                {
                    "status": status,
                    "attempts": attempts,
                    "run_after": run_after.isoformat(),
                    "last_error": error,
                    "updated_at": now.isoformat(),
                }
            )
            .eq("id", job_id)
        )

    def record_message(self, tenant_id: int, msg: NormalizedMessage) -> tuple[int, bool]:
        # Project routing is prompt-only; do not persist project_name on incoming messages.
        payload = {
            "tenant_id": tenant_id,
            "provider": msg.provider,
            "provider_message_id": msg.provider_message_id,
            "received_at": msg.received_at.isoformat(),
            "text": msg.text,
            "raw_json": msg.raw,
            "status": "received",
            "project_name": None,
        }
        try:
            data = self._execute(self._table("messages").insert(payload))
        except (APIError, RuntimeError) as exc:
            if self._is_unique_violation_error(exc):
                data = None
            else:
                raise
        if data:
            return int(data[0]["id"]), True

        existing = self._select_one(
            "messages",
            tenant_id=tenant_id,
            provider_message_id=msg.provider_message_id,
        )
        if not existing:
            raise RuntimeError("message_insert_failed")
        return int(existing["id"]), False

    def update_message_status(self, message_id: int, status: str) -> None:
        self._execute(
            self._table("messages").update({"status": status}).eq("id", message_id)
        )

    def update_message_text(self, message_id: int, text: str | None) -> None:
        self._execute(
            self._table("messages").update({"text": text}).eq("id", message_id)
        )

    def update_message_project(self, message_id: int, project_name: str | None) -> None:
        self._execute(
            self._table("messages")
            .update({"project_name": project_name})
            .eq("id", message_id)
        )

    def record_message_event(
        self,
        *,
        tenant_id: int,
        direction: str,
        provider: str | None,
        tenant_external_id: str | None,
        message_type: str | None,
        text: str | None,
        project_name: str | None,
        run_id: int | None,
        reply_to_message_id: str | None,
        provider_message_id: str | None,
        metadata: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> None:
        now = created_at or datetime.now(tz=timezone.utc).isoformat()
        payload = {
            "tenant_id": tenant_id,
            "direction": direction,
            "provider": provider,
            "tenant_external_id": tenant_external_id,
            "message_type": message_type,
            "text": text,
            "project_name": project_name,
            "run_id": run_id,
            "reply_to_message_id": reply_to_message_id,
            "provider_message_id": provider_message_id,
            "metadata_json": metadata,
            "created_at": now,
        }
        self._execute(self._table("message_events").insert(payload))

    def record_webhook_update(
        self,
        *,
        provider: str,
        update_id: int | None,
        payload: dict[str, Any],
        parse_status: str,
        parse_error: str | None = None,
        tenant_external_id: str | None = None,
        provider_message_id: str | None = None,
        message_text: str | None = None,
    ) -> None:
        row = {
            "provider": provider,
            "update_id": int(update_id) if update_id is not None else None,
            "tenant_external_id": tenant_external_id,
            "provider_message_id": provider_message_id,
            "message_text": message_text,
            "parse_status": parse_status,
            "parse_error": parse_error,
            "payload_json": payload,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        try:
            self._execute(self._table("webhook_updates").insert(row))
        except Exception:
            # Best-effort diagnostics path.
            return

    def list_message_events(
        self,
        tenant_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = self._table("message_events").select("*").order("created_at", desc=False)
        if tenant_id is not None:
            query = query.eq("tenant_id", tenant_id)
        if limit:
            query = query.limit(limit)
        data = self._execute(query)
        return list(data or [])

    def list_messages_since(
        self,
        tenant_id: int,
        since: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            self._table("messages")
            .select("id, received_at")
            .eq("tenant_id", tenant_id)
            .gt("received_at", since)
            .order("received_at", desc=False)
        )
        if limit:
            query = query.limit(limit)
        data = self._execute(query)
        return list(data or [])

    def list_message_events_since(
        self,
        tenant_id: int,
        since: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            self._table("message_events")
            .select("id, created_at")
            .eq("tenant_id", tenant_id)
            .gt("created_at", since)
            .order("created_at", desc=False)
        )
        if limit:
            query = query.limit(limit)
        data = self._execute(query)
        return list(data or [])

    def has_outbound_message_event_since(self, tenant_id: int, since: str) -> bool:
        query = (
            self._table("message_events")
            .select("id")
            .eq("tenant_id", tenant_id)
            .eq("direction", "outbound")
            .gte("created_at", since)
            .limit(1)
        )
        data = self._execute(query)
        return bool(data)

    def has_outbound_message_event_correlation(
        self,
        tenant_id: int,
        correlation_id: str,
        *,
        fallback_scan_limit: int = 200,
    ) -> bool:
        normalized = str(correlation_id or "").strip()
        if not normalized:
            return False
        # Primary path: server-side JSON containment filter.
        try:
            query = (
                self._table("message_events")
                .select("id")
                .eq("tenant_id", int(tenant_id))
                .eq("direction", "outbound")
                .contains("metadata_json", {"correlation_id": normalized})
                .limit(1)
            )
            data = self._execute(query)
            if data:
                return True
        except Exception:
            pass
        # Fallback for older PostgREST/query builders without jsonb contains.
        try:
            rows = self._execute(
                self._table("message_events")
                .select("metadata_json")
                .eq("tenant_id", int(tenant_id))
                .eq("direction", "outbound")
                .order("created_at", desc=True)
                .limit(max(1, int(fallback_scan_limit)))
            )
        except Exception:
            return False
        for row in rows or []:
            metadata = row.get("metadata_json") if isinstance(row, dict) else None
            if not isinstance(metadata, dict):
                continue
            candidate = str(metadata.get("correlation_id") or "").strip()
            if candidate == normalized:
                return True
        return False

    def get_latest_message_received_at(self, tenant_id: int) -> str | None:
        query = (
            self._table("messages")
            .select("received_at")
            .eq("tenant_id", tenant_id)
            .order("received_at", desc=True)
            .limit(1)
        )
        data = self._execute(query)
        if not data:
            return None
        value = data[0].get("received_at")
        return str(value) if value else None

    def get_latest_user_message(self, tenant_id: int) -> dict[str, Any] | None:
        query = (
            self._table("messages")
            .select("id, provider, provider_message_id, received_at, text, status")
            .eq("tenant_id", int(tenant_id))
            .neq("provider", "event")
            .order("received_at", desc=True)
            .limit(1)
        )
        data = self._execute(query)
        if not data:
            return None
        row = data[0]
        return dict(row) if isinstance(row, dict) else None

    def count_tenant_messages(
        self,
        tenant_id: int,
        *,
        statuses: list[str] | None = None,
    ) -> int:
        query = self._table("messages").select("id").eq("tenant_id", int(tenant_id))
        if statuses:
            normalized = [str(item).strip() for item in statuses if str(item).strip()]
            if normalized:
                query = query.in_("status", normalized)
        data = self._execute(query)
        return len(data or [])

    def get_latest_message_event_at(self, tenant_id: int) -> str | None:
        query = (
            self._table("message_events")
            .select("created_at")
            .eq("tenant_id", tenant_id)
            .order("created_at", desc=True)
            .limit(1)
        )
        data = self._execute(query)
        if not data:
            return None
        value = data[0].get("created_at")
        return str(value) if value else None

    def get_next_pending_message(
        self, tenant_id: int, project_name: str | None = None
    ) -> dict[str, Any] | None:
        query = (
            self._table("messages")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("status", "pending")
        )
        if project_name:
            query = query.eq("project_name", project_name)
        data = self._execute(query.order("received_at", desc=False).limit(1))
        return data[0] if data else None

    def get_pending_messages(
        self, tenant_id: int, project_name: str | None = None
    ) -> list[dict[str, Any]]:
        query = (
            self._table("messages")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("status", "pending")
            .order("received_at", desc=False)
        )
        if project_name:
            query = query.eq("project_name", project_name)
        data = self._execute(query)
        return list(data or [])

    def fetch_pending_message_groups(self, limit: int = 25) -> list[dict[str, Any]]:
        data = self._execute(
            self._table("pending_message_groups")
            .select("tenant_id, project_name, oldest_received_at")
            .order("oldest_received_at", desc=False)
            .limit(limit)
        )
        return list(data or [])

    def fetch_stale_received_messages(
        self,
        *,
        tenant_id: int | None = None,
        before: datetime,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        query = (
            self._table("messages")
            .select("*")
            .eq("status", "received")
            .lt("received_at", before.isoformat())
        )
        if tenant_id is not None:
            query = query.eq("tenant_id", int(tenant_id))
        data = self._execute(query.order("received_at", desc=True).limit(limit))
        return list(data or [])

    def fetch_processing_message_groups(self, limit: int = 25) -> list[dict[str, Any]]:
        data = self._execute(
            self._table("processing_message_groups")
            .select("tenant_id, project_name, oldest_received_at")
            .order("oldest_received_at", desc=False)
            .limit(limit)
        )
        return list(data or [])

    def requeue_processing_messages(self, tenant_id: int, project_name: str | None) -> int:
        query = (
            self._table("messages")
            .update({"status": "pending"})
            .eq("tenant_id", tenant_id)
            .eq("status", "processing")
        )
        if project_name:
            query = query.eq("project_name", project_name)
        data = self._execute(query)
        return len(data or [])

    def clear_pending_and_processing_messages(
        self, tenant_id: int, project_name: str | None
    ) -> int:
        query = (
            self._table("messages")
            .update({"status": "processed"})
            .eq("tenant_id", tenant_id)
            .in_("status", ["pending", "processing"])
        )
        if project_name:
            query = query.eq("project_name", project_name)
        data = self._execute(query)
        return len(data or [])

    def finish_running_runs(
        self, tenant_id: int, project_name: str | None, error: str
    ) -> int:
        now = datetime.now(tz=timezone.utc).isoformat()
        query = (
            self._table("runs")
            .update({"status": "failed", "finished_at": now, "error": error})
            .eq("tenant_id", tenant_id)
            .eq("status", "running")
        )
        if project_name:
            query = query.eq("project_name", project_name)
        data = self._execute(query)
        return len(data or [])

    def update_message_statuses(self, message_ids: list[int], status: str) -> None:
        if not message_ids:
            return
        self._execute(
            self._table("messages")
            .update({"status": status})
            .in_("id", message_ids)
        )

    def fetch_messages_by_statuses(
        self,
        tenant_id: int,
        statuses: list[str],
        project_name: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if not statuses:
            return []
        query = (
            self._table("messages")
            .select("*")
            .eq("tenant_id", tenant_id)
            .in_("status", statuses)
            .order("received_at", desc=False)
        )
        if project_name:
            query = query.eq("project_name", project_name)
        if limit:
            query = query.limit(limit)
        data = self._execute(query)
        return list(data or [])

    def has_inflight_run(self, tenant_id: int, project_name: str | None = None) -> bool:
        return self.has_inflight_run_for_role(
            tenant_id=tenant_id,
            project_name=project_name,
            run_role=None,
        )

    def has_inflight_run_for_role(
        self,
        tenant_id: int,
        *,
        project_name: str | None = None,
        run_role: str | None = None,
    ) -> bool:
        normalized_run_role = (
            self._normalize_run_role(run_role)
            if run_role is not None
            else None
        )
        query = (
            self._table("runs")
            .select("id")
            .eq("tenant_id", tenant_id)
            .eq("status", "running")
        )
        if project_name:
            query = query.eq("project_name", project_name)
        if normalized_run_role is not None:
            query = query.eq("run_role", normalized_run_role)
        try:
            data = self._execute(query.limit(1))
        except Exception as exc:
            if normalized_run_role is not None and (
                self._is_schema_cache_missing(exc, "run_role")
                or self._is_missing_column(exc, "runs.run_role")
            ):
                if normalized_run_role != DEFAULT_RUN_ROLE:
                    return False
                return self.has_inflight_run_for_role(
                    tenant_id=tenant_id,
                    project_name=project_name,
                    run_role=None,
                )
            raise
        return bool(data)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        return self._select_one("runs", id=run_id)

    def get_inflight_run(
        self,
        tenant_id: int,
        project_name: str | None = None,
        run_role: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_run_role = (
            self._normalize_run_role(run_role)
            if run_role is not None
            else None
        )
        query = (
            self._table("runs")
            .select("*")
            .eq("tenant_id", tenant_id)
            .eq("status", "running")
            .order("started_at", desc=True)
            .limit(1)
        )
        if project_name:
            query = query.eq("project_name", project_name)
        if normalized_run_role is not None:
            query = query.eq("run_role", normalized_run_role)
        try:
            data = self._execute(query)
        except Exception as exc:
            if normalized_run_role is not None and (
                self._is_schema_cache_missing(exc, "run_role")
                or self._is_missing_column(exc, "runs.run_role")
            ):
                if normalized_run_role != DEFAULT_RUN_ROLE:
                    return None
                return self.get_inflight_run(
                    tenant_id=tenant_id,
                    project_name=project_name,
                    run_role=None,
                )
            raise
        return data[0] if data else None

    def list_tenant_inflight_runs(
        self,
        tenant_id: int,
        *,
        limit: int = 20,
        run_role: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_run_role = (
            self._normalize_run_role(run_role)
            if run_role is not None
            else None
        )
        query = (
            self._table("runs")
            .select("*")
            .eq("tenant_id", int(tenant_id))
            .eq("status", "running")
            .order("started_at", desc=True)
            .limit(max(1, int(limit)))
        )
        if normalized_run_role is not None:
            query = query.eq("run_role", normalized_run_role)
        try:
            data = self._execute(query)
        except Exception as exc:
            if normalized_run_role is not None and (
                self._is_schema_cache_missing(exc, "run_role")
                or self._is_missing_column(exc, "runs.run_role")
            ):
                if normalized_run_role != DEFAULT_RUN_ROLE:
                    return []
                return self.list_tenant_inflight_runs(
                    int(tenant_id),
                    limit=limit,
                    run_role=None,
                )
            raise
        return list(data or [])

    def list_inflight_runs(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        query = (
            self._table("runs")
            .select("*")
            .eq("status", "running")
            # Oldest-first prevents stale-run starvation when concurrency exceeds batch size.
            .order("started_at", desc=False)
        )
        if offset > 0:
            end = offset + max(1, int(limit)) - 1
            query = query.range(offset, end)
        else:
            query = query.limit(limit)
        data = self._execute(query)
        return list(data or [])

    def list_recent_runs(
        self,
        tenant_id: int,
        project_name: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        query = (
            self._table("runs")
            .select(
                "id, status, started_at, finished_at, error, result_summary, tool_summary_json"
            )
            .eq("tenant_id", tenant_id)
            .order("started_at", desc=True)
            .limit(limit)
        )
        if project_name:
            query = query.eq("project_name", project_name)
        data = self._execute(query)
        return list(data or [])

    def count_failed_runs_for_message(
        self,
        *,
        tenant_id: int,
        message_id: int,
        project_name: str | None = None,
    ) -> int:
        query = (
            self._table("runs")
            .select("id")
            .eq("tenant_id", tenant_id)
            .eq("message_id", message_id)
            .eq("status", "failed")
        )
        if project_name:
            query = query.eq("project_name", project_name)
        data = self._execute(query)
        return len(data or [])

    def create_run(
        self,
        tenant_id: int,
        message_id: int | None = None,
        project_name: str | None = None,
        execution_agent_id: int | None = None,
        execution_context: str | None = None,
        run_role: str = DEFAULT_RUN_ROLE,
        lease_seconds: int | None = None,
        task_path: str | None = None,
        session_id: str | None = None,
    ) -> int:
        normalized_run_role = self._normalize_run_role(run_role)
        now_dt = datetime.now(tz=timezone.utc)
        lease_expires = None
        if lease_seconds is not None:
            lease_expires = (now_dt + timedelta(seconds=lease_seconds)).isoformat()
        payload = {
            "tenant_id": tenant_id,
            "message_id": message_id or 0,
            "status": "running",
            "started_at": now_dt.isoformat(),
            "finished_at": None,
            "error": None,
            "total_cost_usd": None,
            "usage_json": None,
            "project_name": project_name,
            "execution_agent_id": execution_agent_id,
            "execution_context": execution_context,
            "run_role": normalized_run_role,
            "task_path": task_path,
            "session_id": session_id,
            "lease_expires_at": lease_expires,
            "last_heartbeat_at": now_dt.isoformat(),
            "last_activity_at": now_dt.isoformat(),
        }
        try:
            data = self._execute(self._table("runs").insert(payload))
        except Exception as exc:
            missing_execution_agent_id = (
                self._is_schema_cache_missing(exc, "execution_agent_id")
                or self._is_missing_column(exc, "runs.execution_agent_id")
            )
            missing_execution_context = (
                self._is_schema_cache_missing(exc, "execution_context")
                or self._is_missing_column(exc, "runs.execution_context")
            )
            missing_run_role = (
                self._is_schema_cache_missing(exc, "run_role")
                or self._is_missing_column(exc, "runs.run_role")
            )
            if not (missing_execution_agent_id or missing_execution_context or missing_run_role):
                raise
            legacy_payload = dict(payload)
            if missing_execution_agent_id:
                legacy_payload.pop("execution_agent_id", None)
            if missing_execution_context:
                legacy_payload.pop("execution_context", None)
            if missing_run_role:
                legacy_payload.pop("run_role", None)
            data = self._execute(self._table("runs").insert(legacy_payload))
        if not data:
            raise RuntimeError("run_create_failed")
        return int(data[0]["id"])

    def update_run_context(
        self,
        run_id: int,
        *,
        task_path: str | None = None,
        session_id: str | None = None,
        execution_agent_id: int | None = None,
        execution_context: str | None = None,
        run_role: str | None = None,
    ) -> None:
        updates: dict[str, Any] = {}
        if task_path is not None:
            updates["task_path"] = task_path
        if session_id is not None:
            updates["session_id"] = session_id
        if execution_agent_id is not None:
            updates["execution_agent_id"] = int(execution_agent_id)
        if execution_context is not None:
            updates["execution_context"] = str(execution_context or "").strip() or None
        if run_role is not None:
            updates["run_role"] = self._normalize_run_role(run_role)
        if not updates:
            return
        try:
            self._execute(self._table("runs").update(updates).eq("id", run_id))
        except Exception as exc:
            missing_execution_agent_id = (
                self._is_schema_cache_missing(exc, "execution_agent_id")
                or self._is_missing_column(exc, "runs.execution_agent_id")
            )
            missing_execution_context = (
                self._is_schema_cache_missing(exc, "execution_context")
                or self._is_missing_column(exc, "runs.execution_context")
            )
            missing_run_role = (
                self._is_schema_cache_missing(exc, "run_role")
                or self._is_missing_column(exc, "runs.run_role")
            )
            if not (missing_execution_agent_id or missing_execution_context or missing_run_role):
                raise
            legacy_updates = dict(updates)
            if missing_execution_agent_id:
                legacy_updates.pop("execution_agent_id", None)
            if missing_execution_context:
                legacy_updates.pop("execution_context", None)
            if missing_run_role:
                legacy_updates.pop("run_role", None)
            if not legacy_updates:
                return
            self._execute(self._table("runs").update(legacy_updates).eq("id", run_id))

    def update_run_timestamps(
        self,
        run_id: int,
        *,
        started_at: str | None = None,
        lease_expires_at: str | None = None,
        last_heartbeat_at: str | None = None,
        last_activity_at: str | None = None,
    ) -> None:
        updates: dict[str, Any] = {}
        if started_at is not None:
            updates["started_at"] = started_at
        if lease_expires_at is not None:
            updates["lease_expires_at"] = lease_expires_at
        if last_heartbeat_at is not None:
            updates["last_heartbeat_at"] = last_heartbeat_at
        if last_activity_at is not None:
            updates["last_activity_at"] = last_activity_at
        if not updates:
            return
        self._execute(self._table("runs").update(updates).eq("id", run_id))

    def set_run_final_sent(self, run_id: int) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._execute(
            self._table("runs")
            .update({"final_sent_at": now})
            .eq("id", run_id)
        )

    def is_run_final_sent(self, run_id: int) -> bool:
        row = self._select_one("runs", id=run_id)
        if not row:
            return False
        return bool(row.get("final_sent_at"))

    def get_message(self, message_id: int) -> dict[str, Any] | None:
        return self._select_one("messages", id=message_id)

    def get_message_by_provider_id(
        self, tenant_id: int, provider_message_id: str
    ) -> dict[str, Any] | None:
        return self._select_one(
            "messages",
            tenant_id=tenant_id,
            provider_message_id=provider_message_id,
        )

    def set_tenant_kv(
        self, tenant_id: int, namespace: str, key: str, value: dict[str, Any] | None
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        payload = {
            "tenant_id": tenant_id,
            "namespace": namespace,
            "key": key,
            "value_json": value,
            "updated_at": now,
        }
        self._execute(
            self._table("tenant_state")
            .upsert(payload, on_conflict="tenant_id,namespace,key")
        )

    def get_tenant_kv(self, tenant_id: int, namespace: str, key: str) -> dict[str, Any] | None:
        row = (
            self._table("tenant_state")
            .select("value_json")
            .eq("tenant_id", tenant_id)
            .eq("namespace", namespace)
            .eq("key", key)
            .limit(1)
        )
        data = self._execute(row)
        if not data:
            return None
        payload = data[0].get("value_json")
        return payload if isinstance(payload, dict) else None

    def list_tenant_kv_namespace(
        self,
        tenant_id: int,
        namespace: str,
        key_prefix: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = (
            self._table("tenant_state")
            .select("key,value_json,updated_at")
            .eq("tenant_id", tenant_id)
            .eq("namespace", namespace)
            .order("key", desc=False)
            .limit(limit)
        )
        if key_prefix:
            query = query.like("key", f"{key_prefix}%")
        data = self._execute(query)
        return list(data or [])

    def record_tenant_event(self, tenant_id: int, event_type: str, payload: dict[str, Any]) -> int:
        row = {
            "tenant_id": tenant_id,
            "event_type": event_type,
            "payload_json": payload,
            "received_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        data = self._execute(self._table("tenant_events").insert(row))
        if not data:
            raise RuntimeError("tenant_event_create_failed")
        return int(data[0]["id"])

    def list_tenant_events(
        self,
        tenant_id: int,
        event_type: str | None = None,
        after_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = (
            self._table("tenant_events")
            .select("id,event_type,payload_json,received_at")
            .eq("tenant_id", tenant_id)
            .order("id", desc=False)
            .limit(limit)
        )
        if event_type:
            query = query.eq("event_type", event_type)
        if after_id is not None and int(after_id) > 0:
            query = query.gt("id", int(after_id))
        data = self._execute(query)
        return list(data or [])

    def create_interaction_session(
        self,
        *,
        tenant_id: int,
        project_name: str | None = None,
        status: str = "running",
    ) -> int | None:
        payload = {
            "tenant_id": tenant_id,
            "project_name": project_name,
            "status": status,
            "started_at": datetime.now(tz=timezone.utc).isoformat(),
            "finished_at": None,
        }
        try:
            data = self._execute(self._table("interaction_sessions").insert(payload))
        except Exception:
            return None
        if not data:
            return None
        try:
            return int(data[0]["id"])
        except Exception:
            return None

    def finish_interaction_session(self, session_id: int, *, status: str = "completed") -> None:
        payload = {
            "status": status,
            "finished_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        try:
            self._execute(self._table("interaction_sessions").update(payload).eq("id", int(session_id)))
        except Exception:
            return

    def record_interaction_session_input(
        self,
        *,
        session_id: int,
        message_id: int | None,
        provider_message_id: str | None,
        text: str,
        assets: list[str] | None = None,
        status: str = "streamed",
    ) -> str | None:
        payload = {
            "session_id": int(session_id),
            "message_id": message_id,
            "provider_message_id": provider_message_id,
            "text": text,
            "assets_json": assets or [],
            "status": status,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "streamed_at": datetime.now(tz=timezone.utc).isoformat()
            if status == "streamed"
            else None,
        }
        try:
            data = self._execute(self._table("interaction_session_inputs").insert(payload))
        except Exception:
            return None
        if not data:
            return None
        return str(data[0].get("id") or "")

    def enqueue_execution_stream_input(
        self,
        *,
        tenant_id: int,
        run_id: int,
        text: str,
        project_name: str | None = None,
        message_id: int | None = None,
        provider_message_id: str | None = None,
        assets: list[str] | None = None,
        status: str = "pending",
    ) -> str | None:
        payload = {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "project_name": project_name,
            "message_id": message_id,
            "provider_message_id": provider_message_id,
            "text": text,
            "assets_json": assets or [],
            "status": status,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "streamed_at": None,
        }
        try:
            data = self._execute(self._table("execution_stream_inputs").insert(payload))
        except Exception:
            fallback_id = f"fallback-{uuid.uuid4().hex}"
            fallback_row = {
                **payload,
                "id": fallback_id,
            }
            self._execution_stream_fallback.setdefault(int(run_id), []).append(fallback_row)
            # Caller must switch to shared file fallback when DB persistence fails.
            return None
        if not data:
            return None
        return str(data[0].get("id") or "")

    def list_execution_stream_inputs(
        self,
        *,
        run_id: int | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        try:
            query = self._table("execution_stream_inputs").select("*").order("created_at", desc=False)
            if run_id is not None:
                query = query.eq("run_id", int(run_id))
            if status:
                query = query.eq("status", status)
            if limit:
                query = query.limit(limit)
            data = self._execute(query)
        except Exception:
            rows: list[dict[str, Any]] = []
            if run_id is not None:
                rows = list(self._execution_stream_fallback.get(int(run_id), []))
            else:
                for values in self._execution_stream_fallback.values():
                    rows.extend(values)
            if status:
                rows = [row for row in rows if str(row.get("status") or "") == status]
            if limit:
                rows = rows[:limit]
            return rows
        return list(data or [])

    def claim_execution_stream_inputs(self, *, run_id: int, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.list_execution_stream_inputs(run_id=run_id, status="pending", limit=limit)
        if not rows:
            return []
        ids = [row.get("id") for row in rows if row.get("id")]
        if not ids:
            return []
        try:
            self._execute(
                self._table("execution_stream_inputs")
                .update(
                    {
                        "status": "streamed",
                        "streamed_at": datetime.now(tz=timezone.utc).isoformat(),
                    }
                )
                .in_("id", ids)
            )
        except Exception:
            id_set = {str(item) for item in ids}
            for key in list(self._execution_stream_fallback.keys()):
                values = self._execution_stream_fallback.get(key) or []
                keep: list[dict[str, Any]] = []
                for row in values:
                    row_id = str(row.get("id") or "")
                    if row_id in id_set:
                        updated = dict(row)
                        updated["status"] = "streamed"
                        updated["streamed_at"] = datetime.now(tz=timezone.utc).isoformat()
                        keep.append(updated)
                    else:
                        keep.append(row)
                self._execution_stream_fallback[key] = keep
            return rows
        return rows

    def create_run_input(
        self,
        tenant_id: int,
        run_id: int | None,
        source: str,
        provider_message_id: str | None,
        payload: dict[str, Any],
        status: str = "queued",
        project_name: str | None = None,
    ) -> str:
        now = datetime.now(tz=timezone.utc).isoformat()
        payload_row = {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "project_name": project_name,
            "source": source,
            "provider_message_id": provider_message_id,
            "payload_json": payload,
            "status": status,
            "claimed_at": None,
            "handled_at": None,
            "created_at": now,
        }
        data = self._execute(self._table("run_inputs").insert(payload_row))
        if not data:
            row = self._select_one(
                "run_inputs",
                tenant_id=tenant_id,
                provider_message_id=provider_message_id,
            )
            if not row:
                raise RuntimeError("run_input_create_failed")
            return str(row["id"])
        return str(data[0]["id"])

    def claim_run_inputs_for_project(
        self, tenant_id: int, project_name: str | None, limit: int = 10
    ) -> list[dict[str, Any]]:
        params = {
            "p_tenant_id": tenant_id,
            "p_project_name": project_name,
            "p_limit": limit,
        }
        data = self._execute(self.client.rpc("claim_run_inputs_for_project", params))
        return list(data or [])

    def update_run_inputs_statuses(self, ids: list[str], status: str) -> None:
        if not ids:
            return
        payload: dict[str, Any] = {"status": status}
        if status == "handled":
            payload["handled_at"] = datetime.now(tz=timezone.utc).isoformat()
        if status == "queued":
            payload["claimed_at"] = None
        self._execute(self._table("run_inputs").update(payload).in_("id", ids))

    def cancel_run_inputs(self, tenant_id: int, project_name: str | None) -> int:
        query = (
            self._table("run_inputs")
            .update({"status": "cancelled"})
            .eq("tenant_id", tenant_id)
            .in_("status", ["queued", "claimed"])
        )
        if project_name:
            query = query.eq("project_name", project_name)
        data = self._execute(query)
        return len(data or [])

    def cancel_run_inputs_for_run(self, run_id: int) -> int:
        data = self._execute(
            self._table("run_inputs")
            .update({"status": "cancelled"})
            .eq("run_id", run_id)
            .in_("status", ["queued", "claimed"])
        )
        return len(data or [])

    def fetch_run_inputs(
        self,
        tenant_id: int,
        project_name: str | None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            self._table("run_inputs")
            .select("*")
            .eq("tenant_id", tenant_id)
            .order("created_at", desc=False)
        )
        if project_name:
            query = query.eq("project_name", project_name)
        if status:
            query = query.eq("status", status)
        if limit:
            query = query.limit(limit)
        data = self._execute(query)
        return list(data or [])

    def count_run_inputs(
        self,
        tenant_id: int,
        project_name: str | None = None,
        status: str | None = None,
    ) -> int:
        query = self._table("run_inputs").select("id").eq("tenant_id", tenant_id)
        if project_name:
            query = query.eq("project_name", project_name)
        if status:
            query = query.eq("status", status)
        data = self._execute(query)
        return len(data or [])

    def fetch_queued_run_input_groups(self, limit: int = 25) -> list[dict[str, Any]]:
        data = self._execute(
            self._table("queued_run_input_groups")
            .select("tenant_id, project_name, oldest_created_at")
            .order("oldest_created_at", desc=False)
            .limit(limit)
        )
        return list(data or [])

    def enqueue_outbox(
        self,
        tenant_id: int,
        run_id: int | None,
        project_name: str | None,
        correlation_id: str,
        payload: dict[str, Any],
    ) -> str:
        payload_row = {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "project_name": project_name,
            "correlation_id": correlation_id,
            "payload_json": payload,
            "status": "queued",
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "sent_at": None,
        }
        try:
            data = self._execute(self._table("outbox").insert(payload_row))
        except (APIError, RuntimeError) as exc:
            if self._is_unique_violation_error(exc):
                data = None
            else:
                raise
        if not data:
            row = self._select_one(
                "outbox",
                tenant_id=tenant_id,
                correlation_id=correlation_id,
            )
            if not row:
                raise RuntimeError("outbox_enqueue_failed")
            return str(row["id"])
        return str(data[0]["id"])

    def claim_outbox(
        self,
        limit: int = 25,
        stale_sending_seconds: int | None = None,
    ) -> list[dict[str, Any]]:
        args: dict[str, Any] = {"p_limit": limit}
        if stale_sending_seconds is not None:
            args["p_stale_sending_seconds"] = int(stale_sending_seconds)
        try:
            data = self._execute(self.client.rpc("claim_outbox", args))
        except Exception:
            # Backward compatibility while the DB function rollout catches up.
            data = self._execute(self.client.rpc("claim_outbox", {"p_limit": limit}))
        return list(data or [])

    def list_outbox(
        self,
        tenant_id: int | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = self._table("outbox").select("*").order("created_at", desc=False)
        if tenant_id is not None:
            query = query.eq("tenant_id", tenant_id)
        if status:
            query = query.eq("status", status)
        if limit:
            query = query.limit(limit)
        data = self._execute(query)
        return list(data or [])

    def update_outbox_statuses(self, ids: list[str], status: str) -> None:
        if not ids:
            return
        payload: dict[str, Any] = {"status": status}
        if status == "sent":
            payload["sent_at"] = datetime.now(tz=timezone.utc).isoformat()
        self._execute(self._table("outbox").update(payload).in_("id", ids))

    def update_outbox_status(
        self,
        outbox_id: str,
        status: str,
        error: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        now = datetime.now(tz=timezone.utc)
        payload: dict[str, Any] = {
            "status": status,
            "updated_at": now.isoformat(),
        }
        if status == "sent":
            payload["sent_at"] = now.isoformat()
            payload["last_error"] = None
            payload["next_retry_at"] = None
        else:
            payload["last_error"] = error
            if retry_after_seconds is not None:
                payload["next_retry_at"] = (
                    now + timedelta(seconds=float(retry_after_seconds))
                ).isoformat()
            elif status in {"failed", "sending"}:
                payload["next_retry_at"] = None
        if status in {"queued", "failed"}:
            row = self._select_one("outbox", id=outbox_id) or {}
            current_attempts = int(row.get("attempt_count") or 0)
            payload["attempt_count"] = (
                current_attempts + 1 if status == "queued" else current_attempts
            )
        try:
            self._execute(self._table("outbox").update(payload).eq("id", outbox_id))
        except Exception:
            # Backward compatibility for older schemas without retry metadata.
            minimal: dict[str, Any] = {"status": status}
            if status == "sent":
                minimal["sent_at"] = now.isoformat()
            self._execute(self._table("outbox").update(minimal).eq("id", outbox_id))

    def set_active_run(
        self,
        tenant_id: int,
        project_name: str,
        run_id: int,
        lease_expires_at: str | None,
    ) -> None:
        payload = {
            "tenant_id": tenant_id,
            "project_name": project_name,
            "run_id": run_id,
            "lease_expires_at": lease_expires_at,
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        self._execute(self._table("active_runs").upsert(payload))

    def get_active_run(
        self, tenant_id: int, project_name: str | None
    ) -> dict[str, Any] | None:
        query = (
            self._table("active_runs")
            .select("*")
            .eq("tenant_id", tenant_id)
            .limit(1)
        )
        if project_name:
            query = query.eq("project_name", project_name)
        data = self._execute(query)
        return data[0] if data else None

    def list_active_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        data = self._execute(
            self._table("active_runs")
            .select("*")
            .order("updated_at", desc=False)
            .limit(limit)
        )
        return list(data or [])

    def clear_active_run(self, tenant_id: int, project_name: str | None) -> None:
        query = self._table("active_runs").delete().eq("tenant_id", tenant_id)
        if project_name:
            query = query.eq("project_name", project_name)
        self._execute(query)

    def update_run_lease(
        self,
        run_id: int,
        lease_expires_at: str | None,
        last_activity_at: str | None = None,
        last_heartbeat_at: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"lease_expires_at": lease_expires_at}
        if last_activity_at is not None:
            payload["last_activity_at"] = last_activity_at
        if last_heartbeat_at is not None:
            payload["last_heartbeat_at"] = last_heartbeat_at
        self._execute(self._table("runs").update(payload).eq("id", run_id))

    def expire_stale_runs(
        self,
        tenant_id: int,
        project_name: str | None,
        now: datetime,
        run_role: str | None = None,
    ) -> int:
        normalized_run_role = (
            self._normalize_run_role(run_role)
            if run_role is not None
            else None
        )
        now_str = now.isoformat()
        query = (
            self._table("runs")
            .update({"status": "failed", "finished_at": now_str, "error": "lease_expired"})
            .eq("tenant_id", tenant_id)
            .eq("status", "running")
            .lte("lease_expires_at", now_str)
        )
        if project_name:
            query = query.eq("project_name", project_name)
        if normalized_run_role is not None:
            query = query.eq("run_role", normalized_run_role)
        try:
            data = self._execute(query)
        except Exception as exc:
            if normalized_run_role is not None and (
                self._is_schema_cache_missing(exc, "run_role")
                or self._is_missing_column(exc, "runs.run_role")
            ):
                if normalized_run_role != DEFAULT_RUN_ROLE:
                    return 0
                return self.expire_stale_runs(
                    tenant_id=tenant_id,
                    project_name=project_name,
                    now=now,
                    run_role=None,
                )
            raise
        return len(data or [])

    def finish_run(
        self,
        run_id: int,
        status: str = "completed",
        error: str | None = None,
        *,
        expected_current_status: str | None = None,
    ) -> bool:
        now = datetime.now(tz=timezone.utc).isoformat()
        query = self._table("runs").update({"status": status, "finished_at": now, "error": error}).eq(
            "id", run_id
        )
        if expected_current_status:
            query = query.eq("status", expected_current_status)
        data = self._execute(query)
        return bool(data)

    def update_run_usage(
        self,
        run_id: int,
        total_cost_usd: float | None = None,
        usage: dict | None = None,
    ) -> None:
        if total_cost_usd is None and not usage:
            return
        payload: dict[str, Any] = {"total_cost_usd": total_cost_usd, "usage_json": usage}
        self._execute(self._table("runs").update(payload).eq("id", run_id))

    def update_run_decision(self, run_id: int, decision: dict[str, Any] | None) -> None:
        if decision is None:
            return
        self._execute(
            self._table("runs")
            .update({"interaction_decision_json": decision})
            .eq("id", run_id)
        )

    def update_run_result_summary(self, run_id: int, summary: str | None) -> None:
        if summary is None:
            return
        self._execute(self._table("runs").update({"result_summary": summary}).eq("id", run_id))

    def update_run_tool_summary(self, run_id: int, summary: dict[str, Any] | None) -> None:
        if summary is None:
            return
        self._execute(
            self._table("runs").update({"tool_summary_json": summary}).eq("id", run_id)
        )

    def update_run_tool_runs(self, run_id: int, tool_runs: list[dict[str, Any]] | None) -> None:
        if tool_runs is None:
            return
        self._execute(self._table("runs").update({"tool_runs_json": tool_runs}).eq("id", run_id))

    @staticmethod
    def _looks_like_raw_usage(payload: dict[str, Any]) -> bool:
        token_keys = {
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "service_tier",
            "server_tool_use",
            "cache_creation",
        }
        return any(key in payload for key in token_keys)

    @classmethod
    def _wrap_usage_payload(cls, payload: Any | None) -> dict[str, Any]:
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            return {"primary": payload}
        if any(key in payload for key in ("primary", "interaction", "interactions")):
            return dict(payload)
        if cls._looks_like_raw_usage(payload):
            return {"primary": payload}
        return dict(payload)

    @classmethod
    def _merge_usage_payload(
        cls,
        existing: Any | None,
        additional: dict[str, Any],
        usage_key: str | None,
    ) -> dict[str, Any]:
        payload = cls._wrap_usage_payload(existing)
        if usage_key:
            bucket = payload.get(usage_key)
            if bucket is None:
                payload[usage_key] = [additional]
            elif isinstance(bucket, list):
                bucket.append(additional)
            else:
                payload[usage_key] = [bucket, additional]
        else:
            payload["additional"] = additional
        return payload

    def add_run_usage(
        self,
        run_id: int,
        total_cost_usd: float | None = None,
        usage: dict | None = None,
        usage_key: str | None = "interaction",
    ) -> None:
        if total_cost_usd is None and not usage:
            return
        row = self._select_one("runs", id=run_id)
        if not row:
            return
        existing_total = row.get("total_cost_usd")
        new_total = existing_total
        if total_cost_usd is not None:
            base = existing_total or 0.0
            new_total = base + total_cost_usd
        existing_usage = row.get("usage_json")
        merged_usage = existing_usage
        if usage is not None:
            merged_usage = self._merge_usage_payload(existing_usage, usage, usage_key)
        payload: dict[str, Any] = {"total_cost_usd": new_total, "usage_json": merged_usage}
        self._execute(self._table("runs").update(payload).eq("id", run_id))

    def create_billing_order(
        self,
        tenant_id: int,
        order_type: str,
        status: str,
        price_usd: float | None = None,
        currency: str | None = None,
        metadata: dict | None = None,
        error: str | None = None,
    ) -> int:
        now = datetime.now(tz=timezone.utc).isoformat()
        payload = {
            "tenant_id": tenant_id,
            "order_type": order_type,
            "status": status,
            "price_usd": price_usd,
            "currency": currency,
            "metadata_json": metadata,
            "error": error,
            "created_at": now,
            "updated_at": now,
        }
        data = self._execute(self._table("billing_orders").insert(payload))
        if not data:
            raise RuntimeError("billing_order_create_failed")
        return int(data[0]["id"])

    def update_billing_order_payment(
        self,
        order_id: int,
        stripe_session_id: str | None,
        stripe_payment_url: str | None,
        status: str = "pending_payment",
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        payload = {
            "status": status,
            "stripe_session_id": stripe_session_id,
            "stripe_payment_url": stripe_payment_url,
            "updated_at": now,
        }
        self._execute(self._table("billing_orders").update(payload).eq("id", order_id))

    def mark_billing_order_paid(
        self,
        order_id: int,
        stripe_session_id: str | None = None,
        stripe_subscription_id: str | None = None,
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        payload = {
            "status": "paid",
            "stripe_session_id": stripe_session_id,
            "stripe_subscription_id": stripe_subscription_id,
            "updated_at": now,
        }
        self._execute(self._table("billing_orders").update(payload).eq("id", order_id))

    def mark_billing_order_failed(self, order_id: int, error: str) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        payload = {"status": "failed", "error": error, "updated_at": now}
        self._execute(self._table("billing_orders").update(payload).eq("id", order_id))

    def update_billing_order_status(
        self,
        order_id: int,
        status: str,
        error: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        metadata_json = None
        if metadata:
            current = {}
            row = self._select_one("billing_orders", id=order_id)
            if row and row.get("metadata_json"):
                raw = row.get("metadata_json")
                if isinstance(raw, dict):
                    current = raw
                else:
                    try:
                        current = json.loads(raw)
                    except (TypeError, ValueError):
                        current = {}
            current.update(metadata)
            metadata_json = current

        payload = {"status": status, "error": error, "updated_at": now}
        if metadata_json is not None:
            payload["metadata_json"] = metadata_json
        self._execute(self._table("billing_orders").update(payload).eq("id", order_id))

    def get_billing_order(self, order_id: int) -> dict[str, Any] | None:
        return self._select_one("billing_orders", id=order_id)

    def get_billing_order_by_subscription(self, subscription_id: str) -> dict[str, Any] | None:
        return self._select_one("billing_orders", stripe_subscription_id=subscription_id)

    def get_billing_order_by_session(self, session_id: str) -> dict[str, Any] | None:
        return self._select_one("billing_orders", stripe_session_id=session_id)

    def get_latest_billing_order(
        self,
        tenant_id: int,
        order_type: str | None = None,
    ) -> dict[str, Any] | None:
        query = self._table("billing_orders").select("*").eq("tenant_id", tenant_id)
        if order_type:
            query = query.eq("order_type", order_type)
        data = self._execute(query.order("id", desc=True).limit(1))
        if data:
            return data[0]
        return None

    def get_latest_paid_billing_order(
        self,
        tenant_id: int,
        *,
        order_type_prefix: str | None = None,
        statuses: set[str] | None = None,
    ) -> dict[str, Any] | None:
        status_list = sorted(statuses or {"paid", "active", "cancel_scheduled"})
        query = (
            self._table("billing_orders")
            .select("*")
            .eq("tenant_id", tenant_id)
            .in_("status", status_list)
        )
        if order_type_prefix:
            query = query.like("order_type", f"{order_type_prefix}%")
        data = self._execute(query.order("id", desc=True).limit(1))
        if data:
            return data[0]
        return None

    def sum_run_costs(
        self,
        tenant_id: int,
        *,
        started_after: str | None = None,
    ) -> float:
        query = (
            self._table("runs")
            .select("total:total_cost_usd.sum()")
            .eq("tenant_id", tenant_id)
        )
        if started_after:
            query = query.gte("started_at", started_after)
        data = self._execute(query)
        if not data:
            return 0.0
        row = data[0] if isinstance(data, list) else data
        raw = None
        if isinstance(row, dict):
            raw = row.get("total")
            if raw is None:
                raw = row.get("sum")
            if raw is None:
                raw = row.get("total_cost_usd")
        if raw is None:
            return 0.0
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def upsert_supabase_project(
        self,
        tenant_id: int,
        project_ref: str | None,
        project_id: str | None,
        project_name: str | None,
        region: str | None,
        status: str | None,
        api_url: str | None,
        publishable_key: str | None,
        secret_key: str | None,
        anon_key: str | None,
        service_role_key: str | None,
        raw: dict | None = None,
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        payload = {
            "tenant_id": tenant_id,
            "project_ref": project_ref,
            "project_id": project_id,
            "project_name": project_name,
            "region": region,
            "status": status,
            "api_url": api_url,
            "publishable_key": publishable_key,
            "secret_key": secret_key,
            "anon_key": anon_key,
            "service_role_key": service_role_key,
            "raw_json": raw,
            "created_at": now,
            "updated_at": now,
        }
        self._execute(
            self._table("supabase_projects").upsert(payload, on_conflict="tenant_id")
        )

    def get_supabase_project(self, tenant_id: int) -> dict[str, Any] | None:
        return self._select_one("supabase_projects", tenant_id=tenant_id)

    def create_pm_heartbeat(
        self,
        *,
        tenant_id: int,
        trigger_type: str,
        trigger_event_id: int | None = None,
        triage_result: dict[str, Any] | None = None,
        action_needed: bool = False,
        triage_cost_usd: float | None = None,
        triage_model: str | None = None,
        action_result: dict[str, Any] | None = None,
        action_cost_usd: float | None = None,
        action_model: str | None = None,
        total_cost_usd: float | None = None,
        completed_at: str | None = None,
    ) -> int | None:
        payload = {
            "tenant_id": int(tenant_id),
            "trigger_type": str(trigger_type or "").strip() or "unknown",
            "trigger_event_id": trigger_event_id,
            "triage_result": triage_result,
            "action_needed": bool(action_needed),
            "triage_cost_usd": triage_cost_usd,
            "triage_model": triage_model,
            "action_result": action_result,
            "action_cost_usd": action_cost_usd,
            "action_model": action_model,
            "total_cost_usd": float(total_cost_usd or 0.0),
            "completed_at": completed_at,
        }
        try:
            data = self._execute(self._table("pm_heartbeats").insert(payload))
        except Exception as exc:
            if self._is_missing_relation(exc, "pm_heartbeats"):
                return None
            raise
        if not data:
            return None
        try:
            return int(data[0]["id"])
        except Exception:
            return None

    def update_pm_heartbeat(
        self,
        heartbeat_id: int,
        *,
        triage_result: dict[str, Any] | None = None,
        action_needed: bool | None = None,
        triage_cost_usd: float | None = None,
        triage_model: str | None = None,
        action_result: dict[str, Any] | None = None,
        action_cost_usd: float | None = None,
        action_model: str | None = None,
        total_cost_usd: float | None = None,
        completed_at: str | None = None,
    ) -> None:
        updates: dict[str, Any] = {}
        if triage_result is not None:
            updates["triage_result"] = triage_result
        if action_needed is not None:
            updates["action_needed"] = bool(action_needed)
        if triage_cost_usd is not None:
            updates["triage_cost_usd"] = triage_cost_usd
        if triage_model is not None:
            updates["triage_model"] = triage_model
        if action_result is not None:
            updates["action_result"] = action_result
        if action_cost_usd is not None:
            updates["action_cost_usd"] = action_cost_usd
        if action_model is not None:
            updates["action_model"] = action_model
        if total_cost_usd is not None:
            updates["total_cost_usd"] = total_cost_usd
        if completed_at is not None:
            updates["completed_at"] = completed_at
        if not updates:
            return
        try:
            self._execute(self._table("pm_heartbeats").update(updates).eq("id", int(heartbeat_id)))
        except Exception as exc:
            if self._is_missing_relation(exc, "pm_heartbeats"):
                return
            raise

    def create_pm_action(
        self,
        *,
        tenant_id: int,
        heartbeat_id: int | None,
        action_type: str,
        autonomy_tier: str,
        description: str,
        execution_run_id: int | None = None,
        outbox_id: str | None = None,
        status: str = "pending",
        result_summary: str | None = None,
    ) -> int | None:
        now = datetime.now(tz=timezone.utc).isoformat()
        payload = {
            "tenant_id": int(tenant_id),
            "heartbeat_id": heartbeat_id,
            "action_type": str(action_type or "").strip() or "unknown",
            "autonomy_tier": str(autonomy_tier or "").strip() or "auto",
            "description": str(description or "").strip(),
            "execution_run_id": execution_run_id,
            "outbox_id": outbox_id,
            "status": str(status or "pending").strip() or "pending",
            "result_summary": result_summary,
            "created_at": now,
            "updated_at": now,
        }
        try:
            data = self._execute(self._table("pm_actions").insert(payload))
        except Exception as exc:
            if self._is_missing_relation(exc, "pm_actions"):
                return None
            raise
        if not data:
            return None
        try:
            return int(data[0]["id"])
        except Exception:
            return None

    def update_pm_action(
        self,
        action_id: int,
        *,
        status: str | None = None,
        result_summary: str | None = None,
        user_response: str | None = None,
        user_response_at: str | None = None,
        execution_run_id: int | None = None,
        outbox_id: str | None = None,
    ) -> None:
        updates: dict[str, Any] = {"updated_at": datetime.now(tz=timezone.utc).isoformat()}
        if status is not None:
            updates["status"] = str(status).strip() or "pending"
        if result_summary is not None:
            updates["result_summary"] = result_summary
        if user_response is not None:
            updates["user_response"] = user_response
        if user_response_at is not None:
            updates["user_response_at"] = user_response_at
        if execution_run_id is not None:
            updates["execution_run_id"] = int(execution_run_id)
        if outbox_id is not None:
            updates["outbox_id"] = outbox_id
        try:
            self._execute(self._table("pm_actions").update(updates).eq("id", int(action_id)))
        except Exception as exc:
            if self._is_missing_relation(exc, "pm_actions"):
                return
            raise

    # ------------------------------------------------------------------
    # AgentMail helpers
    # ------------------------------------------------------------------

    def get_tenant_by_agentmail_pod(self, pod_id: str) -> Tenant | None:
        """Look up a tenant by their AgentMail pod_id stored in tenant_state KV."""
        pod_value = str(pod_id or "").strip()
        if not pod_value:
            return None
        data = self._execute(
            self._table("tenant_state")
            .select("tenant_id")
            .eq("namespace", "agentmail")
            .eq("key", "pod")
            .filter("value_json->>pod_id", "eq", pod_value)
            .order("updated_at", desc=True)
            .order("tenant_id", desc=True)
            .limit(1)
        )
        if not data:
            return None
        tenant_id = data[0].get("tenant_id")
        if tenant_id is None:
            return None
        return self.get_tenant_by_id(int(tenant_id))

    def set_agentmail_thread_tenant(self, thread_id: str, tenant_id: int) -> None:
        """Map an AgentMail thread to a tenant so inbound replies can be routed."""
        self.set_tenant_kv(tenant_id, "agentmail", f"thread:{thread_id}", {
            "thread_id": thread_id,
        })

    def get_tenant_by_agentmail_thread(self, thread_id: str) -> Tenant | None:
        """Look up a tenant by an AgentMail thread_id stored in tenant_state KV."""
        key = f"thread:{thread_id}"
        data = self._execute(
            self._table("tenant_state")
            .select("tenant_id")
            .eq("namespace", "agentmail")
            .eq("key", key)
            .order("updated_at", desc=True)
            .order("tenant_id", desc=True)
            .limit(1)
        )
        if not data:
            return None
        tenant_id = data[0].get("tenant_id")
        if tenant_id is None:
            return None
        return self.get_tenant_by_id(int(tenant_id))
