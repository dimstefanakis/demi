from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import json
from typing import Any

from supabase import Client, create_client

from claudius.models import NormalizedMessage, Tenant


@dataclass
class SupabaseDatabase:
    url: str
    service_key: str
    client: Client | None = None

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

    def list_tenants(self) -> list[Tenant]:
        data = self._execute(self._table("tenants").select("*"))
        return [self._row_to_tenant(row) for row in (data or [])]

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

    def fetch_pending_event_jobs(self, limit: int = 25) -> list[dict[str, Any]]:
        now = datetime.now(tz=timezone.utc).isoformat()
        data = self._execute(
            self._table("event_jobs")
            .select("*")
            .eq("status", "pending")
            .lte("run_after", now)
            .order("id", desc=False)
            .limit(limit)
        )
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
        existing = self._select_one(
            "messages",
            tenant_id=tenant_id,
            provider_message_id=msg.provider_message_id,
        )
        if existing:
            return int(existing["id"]), False

        payload = {
            "tenant_id": tenant_id,
            "provider": msg.provider,
            "provider_message_id": msg.provider_message_id,
            "received_at": msg.received_at.isoformat(),
            "text": msg.text,
            "raw_json": msg.raw,
            "status": "received",
            "project_name": msg.project_name,
        }
        data = self._execute(self._table("messages").insert(payload))
        if not data:
            raise RuntimeError("message_insert_failed")
        return int(data[0]["id"]), True

    def update_message_status(self, message_id: int, status: str) -> None:
        self._execute(
            self._table("messages").update({"status": status}).eq("id", message_id)
        )

    def update_message_project(self, message_id: int, project_name: str | None) -> None:
        self._execute(
            self._table("messages")
            .update({"project_name": project_name})
            .eq("id", message_id)
        )

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
        data = self._execute(query)
        return list(data or [])

    def has_inflight_run(self, tenant_id: int, project_name: str | None = None) -> bool:
        query = (
            self._table("runs")
            .select("id")
            .eq("tenant_id", tenant_id)
            .eq("status", "running")
        )
        if project_name:
            query = query.eq("project_name", project_name)
        data = self._execute(query.limit(1))
        return bool(data)

    def get_inflight_run(
        self, tenant_id: int, project_name: str | None = None
    ) -> dict[str, Any] | None:
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
        data = self._execute(query)
        return data[0] if data else None

    def create_run(
        self,
        tenant_id: int,
        message_id: int | None = None,
        project_name: str | None = None,
        lease_seconds: int | None = None,
    ) -> int:
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
            "lease_expires_at": lease_expires,
            "last_heartbeat_at": now_dt.isoformat(),
            "last_activity_at": now_dt.isoformat(),
        }
        data = self._execute(self._table("runs").insert(payload))
        if not data:
            raise RuntimeError("run_create_failed")
        return int(data[0]["id"])

    def create_run_input(
        self,
        tenant_id: int,
        run_id: int | None,
        project_name: str | None,
        source: str,
        provider_message_id: str | None,
        payload: dict[str, Any],
        status: str = "queued",
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
        data = self._execute(self._table("outbox").insert(payload_row))
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

    def claim_outbox(self, limit: int = 25) -> list[dict[str, Any]]:
        data = self._execute(self.client.rpc("claim_outbox", {"p_limit": limit}))
        return list(data or [])

    def update_outbox_statuses(self, ids: list[str], status: str) -> None:
        if not ids:
            return
        payload: dict[str, Any] = {"status": status}
        if status == "sent":
            payload["sent_at"] = datetime.now(tz=timezone.utc).isoformat()
        self._execute(self._table("outbox").update(payload).in_("id", ids))

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

    def expire_stale_runs(self, tenant_id: int, project_name: str | None, now: datetime) -> int:
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
        data = self._execute(query)
        return len(data or [])

    def finish_run(self, run_id: int, status: str = "completed", error: str | None = None) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        self._execute(
            self._table("runs")
            .update({"status": status, "finished_at": now, "error": error})
            .eq("id", run_id)
        )

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
