from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

from demi.db.core import Database


SCHEDULER_NAMESPACE = "scheduler"
TRIGGER_KEY_PREFIX = "trigger:"
logger = logging.getLogger(__name__)


@dataclass
class SchedulerWorkerConfig:
    poll_interval: float = 5.0
    batch_size: int = 50


@dataclass
class SchedulerWorker:
    db: Database
    config: SchedulerWorkerConfig
    _running: bool = False

    async def _db_call(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def run_forever(self) -> None:
        self._running = True
        idle_streak = 0
        try:
            while self._running:
                try:
                    fired = await self.process_once()
                    if fired <= 0:
                        idle_streak = min(idle_streak + 1, 6)
                        await asyncio.sleep(self.config.poll_interval * (2**idle_streak))
                        continue
                    idle_streak = 0
                    await asyncio.sleep(self.config.poll_interval)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("SchedulerWorker loop failed; continuing")
                    idle_streak = min(idle_streak + 1, 6)
                    await asyncio.sleep(
                        max(0.25, float(self.config.poll_interval)) * (2**idle_streak)
                    )
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    async def process_once(self) -> int:
        now = datetime.now(tz=timezone.utc)
        fired_total = 0
        tenants = await self._db_call(self.db.list_tenants)
        for tenant in tenants:
            fired_total += await self._process_tenant(int(tenant.id), now=now)
        return fired_total

    def stop(self) -> None:
        self._running = False

    async def _process_tenant(self, tenant_id: int, *, now: datetime) -> int:
        rows = await self._db_call(
            self.db.list_tenant_kv_namespace,
            tenant_id,
            SCHEDULER_NAMESPACE,
            TRIGGER_KEY_PREFIX,
            self.config.batch_size,
        )
        fired_total = 0
        for row in rows:
            key = str(row.get("key") or "")
            value = row.get("value_json")
            if not key.startswith(TRIGGER_KEY_PREFIX) or not isinstance(value, dict):
                continue
            trigger = dict(value)
            fired_count, updated = await self._evaluate_trigger(
                tenant_id=tenant_id,
                trigger_key=key,
                trigger=trigger,
                now=now,
            )
            fired_total += fired_count
            if updated:
                await self._db_call(
                    self.db.set_tenant_kv,
                    tenant_id,
                    SCHEDULER_NAMESPACE,
                    key,
                    updated,
                )
        return fired_total

    async def _evaluate_trigger(
        self,
        *,
        tenant_id: int,
        trigger_key: str,
        trigger: dict[str, Any],
        now: datetime,
    ) -> tuple[int, dict[str, Any] | None]:
        enabled = bool(trigger.get("enabled", True))
        if not enabled:
            return 0, None
        trigger_type = str(trigger.get("trigger_type") or "").strip().lower()
        state = trigger.get("state")
        if not isinstance(state, dict):
            state = {}
        state = dict(state)
        updated = dict(trigger)
        updated["state"] = state
        fired = 0
        changed = False
        try:
            if trigger_type == "cron":
                fired, changed = await self._evaluate_cron(
                    tenant_id=tenant_id,
                    trigger_key=trigger_key,
                    trigger=updated,
                    now=now,
                )
            elif trigger_type == "webhook_condition":
                fired, changed = await self._evaluate_webhook_condition(
                    tenant_id=tenant_id,
                    trigger_key=trigger_key,
                    trigger=updated,
                    now=now,
                )
            elif trigger_type == "state_change":
                fired, changed = await self._evaluate_state_change(
                    tenant_id=tenant_id,
                    trigger_key=trigger_key,
                    trigger=updated,
                    now=now,
                )
        except Exception as exc:  # noqa: BLE001
            updated_state = updated.get("state") if isinstance(updated.get("state"), dict) else {}
            updated_state = dict(updated_state)
            updated_state["last_error"] = str(exc)
            updated_state["last_error_at"] = now.isoformat()
            updated["state"] = updated_state
            return 0, updated
        if changed:
            updated["updated_at"] = now.isoformat()
            return fired, updated
        return fired, None

    async def _evaluate_cron(
        self,
        *,
        tenant_id: int,
        trigger_key: str,
        trigger: dict[str, Any],
        now: datetime,
    ) -> tuple[int, bool]:
        state = dict(trigger.get("state") or {})
        cron_expr = str(trigger.get("cron") or trigger.get("schedule") or "").strip()
        if not cron_expr:
            return 0, False
        next_run = _parse_datetime(state.get("next_run_at"))
        changed = False
        if next_run is None:
            next_run = _next_cron_occurrence(cron_expr, after=now - timedelta(minutes=1))
            if next_run is None:
                return 0, False
            state["next_run_at"] = next_run.isoformat()
            changed = True
        if next_run > now:
            trigger["state"] = state
            return 0, changed

        run_after = _window_run_after(trigger=trigger, now=now)
        await self._enqueue_trigger_event(
            tenant_id=tenant_id,
            trigger_key=trigger_key,
            trigger=trigger,
            scheduled_at=next_run,
            run_after=run_after,
            source="cron",
            source_payload=None,
        )
        state["last_fired_at"] = now.isoformat()
        next_after = _next_cron_occurrence(cron_expr, after=next_run)
        if next_after is not None:
            state["next_run_at"] = next_after.isoformat()
        trigger["state"] = state
        return 1, True

    async def _evaluate_webhook_condition(
        self,
        *,
        tenant_id: int,
        trigger_key: str,
        trigger: dict[str, Any],
        now: datetime,
    ) -> tuple[int, bool]:
        state = dict(trigger.get("state") or {})
        after_id = _coerce_int(state.get("last_event_id")) or 0
        event_type = str(trigger.get("event_type") or "").strip() or None
        events = await self._db_call(
            self.db.list_tenant_events,
            tenant_id,
            event_type,
            after_id,
            self.config.batch_size,
        )
        if not events:
            return 0, False
        state["last_event_id"] = int(events[-1]["id"])
        fired = 0
        condition = trigger.get("condition")
        for row in events:
            payload = row.get("payload_json")
            if not isinstance(payload, dict):
                continue
            if not _condition_matches(condition, payload):
                continue
            scheduled_at = _parse_datetime(row.get("received_at")) or now
            run_after = _window_run_after(trigger=trigger, now=now)
            await self._enqueue_trigger_event(
                tenant_id=tenant_id,
                trigger_key=trigger_key,
                trigger=trigger,
                scheduled_at=scheduled_at,
                run_after=run_after,
                source="webhook_condition",
                source_payload=payload,
            )
            state["last_fired_at"] = now.isoformat()
            state["last_match_event_id"] = int(row["id"])
            fired += 1
            if not bool(trigger.get("allow_multiple")):
                break
        trigger["state"] = state
        return fired, True

    async def _evaluate_state_change(
        self,
        *,
        tenant_id: int,
        trigger_key: str,
        trigger: dict[str, Any],
        now: datetime,
    ) -> tuple[int, bool]:
        state = dict(trigger.get("state") or {})
        watch = trigger.get("watch")
        if not isinstance(watch, dict):
            watch = {}
        namespace = str(watch.get("namespace") or "").strip()
        key = str(watch.get("key") or "").strip()
        path = str(watch.get("path") or "").strip() or None
        if not namespace or not key:
            return 0, False
        value = await self._db_call(self.db.get_tenant_kv, tenant_id, namespace, key)
        observed = _extract_path(value, path) if path else value
        digest = _stable_hash(observed)
        last_digest = str(state.get("last_seen_hash") or "").strip() or None
        if last_digest is None:
            state["last_seen_hash"] = digest
            state["last_seen_at"] = now.isoformat()
            trigger["state"] = state
            return 0, True
        if last_digest == digest:
            return 0, False

        run_after = _window_run_after(trigger=trigger, now=now)
        source_payload = {
            "namespace": namespace,
            "key": key,
            "path": path,
            "new_value": observed,
        }
        await self._enqueue_trigger_event(
            tenant_id=tenant_id,
            trigger_key=trigger_key,
            trigger=trigger,
            scheduled_at=now,
            run_after=run_after,
            source="state_change",
            source_payload=source_payload,
        )
        state["last_seen_hash"] = digest
        state["last_seen_at"] = now.isoformat()
        state["last_fired_at"] = now.isoformat()
        trigger["state"] = state
        return 1, True

    async def _enqueue_trigger_event(
        self,
        *,
        tenant_id: int,
        trigger_key: str,
        trigger: dict[str, Any],
        scheduled_at: datetime,
        run_after: datetime,
        source: str,
        source_payload: dict[str, Any] | None,
    ) -> None:
        trigger_id = str(trigger.get("id") or trigger_key.replace(TRIGGER_KEY_PREFIX, "")).strip()
        event_type = str(trigger.get("output_event_type") or "scheduled_trigger")
        intent = str(trigger.get("intent") or event_type).strip() or event_type
        event_payload = trigger.get("payload")
        if not isinstance(event_payload, dict):
            event_payload = {}
        event_payload = dict(event_payload)
        if "summary" not in event_payload:
            event_payload["summary"] = str(trigger.get("name") or event_type)
        scheduler_payload = {
            "trigger_id": trigger_id,
            "trigger_type": str(trigger.get("trigger_type") or ""),
            "source": source,
            "scheduled_at": scheduled_at.isoformat(),
            "retry_backoff_seconds": _coerce_int(trigger.get("retry_backoff_seconds")) or 15,
        }
        retry_window = _coerce_int(trigger.get("retry_window_seconds")) or 0
        if retry_window > 0:
            retry_until = datetime.now(tz=timezone.utc) + timedelta(seconds=retry_window)
            scheduler_payload["retry_until"] = retry_until.isoformat()
        if source_payload is not None:
            scheduler_payload["source_payload"] = source_payload
        event_payload["_scheduler"] = scheduler_payload
        event_payload["project_name"] = trigger.get("project_name")
        job_payload = {
            "intent": intent,
            "event_type": event_type,
            "payload": event_payload,
        }
        await self._db_call(
            self.db.create_event_job,
            tenant_id,
            "event",
            job_payload,
            run_after.isoformat(),
        )


@dataclass(frozen=True)
class _CronField:
    values: set[int]
    wildcard: bool


@dataclass(frozen=True)
class _CronExpression:
    minute: _CronField
    hour: _CronField
    day_of_month: _CronField
    month: _CronField
    day_of_week: _CronField


def _next_cron_occurrence(expr: str, *, after: datetime) -> datetime | None:
    parsed = _parse_cron(expr)
    if parsed is None:
        return None
    candidate = after.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = candidate + timedelta(days=366)
    while candidate <= limit:
        if _cron_matches(parsed, candidate):
            return candidate
        candidate += timedelta(minutes=1)
    return None


def _parse_cron(expr: str) -> _CronExpression | None:
    parts = [part.strip() for part in str(expr or "").split() if part.strip()]
    if len(parts) != 5:
        return None
    minute = _parse_cron_field(parts[0], 0, 59)
    hour = _parse_cron_field(parts[1], 0, 23)
    dom = _parse_cron_field(parts[2], 1, 31)
    month = _parse_cron_field(parts[3], 1, 12)
    dow = _parse_cron_field(parts[4], 0, 7, normalize_dow=True)
    if not all((minute, hour, dom, month, dow)):
        return None
    return _CronExpression(
        minute=minute,
        hour=hour,
        day_of_month=dom,
        month=month,
        day_of_week=dow,
    )


def _parse_cron_field(
    value: str,
    minimum: int,
    maximum: int,
    *,
    normalize_dow: bool = False,
) -> _CronField | None:
    text = str(value or "").strip()
    if not text:
        return None
    wildcard = text == "*"
    entries = [entry.strip() for entry in text.split(",") if entry.strip()]
    values: set[int] = set()
    for entry in entries:
        step = 1
        base = entry
        if "/" in entry:
            base, step_value = entry.split("/", 1)
            try:
                step = int(step_value)
            except ValueError:
                return None
            if step <= 0:
                return None
        if base == "*":
            start = minimum
            end = maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            try:
                start = int(start_text)
                end = int(end_text)
            except ValueError:
                return None
        else:
            try:
                start = int(base)
                end = int(base)
            except ValueError:
                return None
        if normalize_dow:
            if start == 7:
                start = 0
            if end == 7:
                end = 0
        if start < minimum or start > maximum:
            return None
        if end < minimum or end > maximum:
            return None
        if start <= end:
            current = start
            while current <= end:
                values.add(current)
                current += step
        else:
            current = start
            while current <= maximum:
                values.add(current)
                current += step
            current = minimum
            while current <= end:
                values.add(current)
                current += step
    if not values:
        return None
    return _CronField(values=values, wildcard=wildcard)


def _cron_matches(parsed: _CronExpression, candidate: datetime) -> bool:
    if candidate.minute not in parsed.minute.values:
        return False
    if candidate.hour not in parsed.hour.values:
        return False
    if candidate.month not in parsed.month.values:
        return False

    dom_match = candidate.day in parsed.day_of_month.values
    dow_value = (candidate.weekday() + 1) % 7
    dow_match = dow_value in parsed.day_of_week.values
    if parsed.day_of_month.wildcard and parsed.day_of_week.wildcard:
        return dom_match and dow_match
    if parsed.day_of_month.wildcard:
        return dow_match
    if parsed.day_of_week.wildcard:
        return dom_match
    return dom_match or dow_match


def _window_run_after(*, trigger: dict[str, Any], now: datetime) -> datetime:
    window = trigger.get("time_window")
    if not isinstance(window, dict):
        window = {}
    start = str(window.get("start") or trigger.get("window_start") or "").strip()
    end = str(window.get("end") or trigger.get("window_end") or "").strip()
    tz_name = str(window.get("timezone") or trigger.get("timezone") or "UTC").strip() or "UTC"
    if not start or not end:
        return now
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        zone = timezone.utc
    start_time = _parse_hhmm(start)
    end_time = _parse_hhmm(end)
    if start_time is None or end_time is None:
        return now
    now_local = now.astimezone(zone)
    if _is_within_window(now_local.time(), start_time, end_time):
        return now
    target_date = now_local.date()
    if now_local.time() > end_time and start_time <= end_time:
        target_date = target_date + timedelta(days=1)
    if start_time > end_time and now_local.time() > end_time and now_local.time() < start_time:
        target_date = target_date
    target_local = datetime.combine(target_date, start_time, tzinfo=zone)
    if target_local <= now_local:
        target_local = target_local + timedelta(days=1)
    return target_local.astimezone(timezone.utc)


def _is_within_window(current: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def _parse_hhmm(raw: str) -> time | None:
    match = re.match(r"^(\d{1,2}):(\d{2})$", raw.strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23:
        return None
    if minute < 0 or minute > 59:
        return None
    return time(hour=hour, minute=minute)


def _condition_matches(condition: Any, payload: dict[str, Any]) -> bool:
    if condition is None:
        return True
    if isinstance(condition, list):
        return all(_condition_matches(item, payload) for item in condition)
    if not isinstance(condition, dict):
        return True
    if "all" in condition and isinstance(condition["all"], list):
        return all(_condition_matches(item, payload) for item in condition["all"])
    if "any" in condition and isinstance(condition["any"], list):
        return any(_condition_matches(item, payload) for item in condition["any"])
    if "none" in condition and isinstance(condition["none"], list):
        return not any(_condition_matches(item, payload) for item in condition["none"])

    path = str(condition.get("path") or "").strip() or None
    value = _extract_path(payload, path) if path else payload
    if "exists" in condition:
        expected = bool(condition.get("exists"))
        return (value is not None) is expected
    if "equals" in condition:
        return value == condition.get("equals")
    if "not_equals" in condition:
        return value != condition.get("not_equals")
    if "contains" in condition:
        needle = condition.get("contains")
        if isinstance(value, str):
            return str(needle) in value
        if isinstance(value, list):
            return needle in value
        return False
    if "in" in condition and isinstance(condition.get("in"), list):
        return value in condition.get("in")
    if "not_in" in condition and isinstance(condition.get("not_in"), list):
        return value not in condition.get("not_in")
    if "regex" in condition:
        pattern = str(condition.get("regex") or "")
        if not pattern:
            return False
        return bool(re.search(pattern, str(value or "")))
    return True


def _extract_path(payload: Any, path: str | None) -> Any:
    if path is None or path == "":
        return payload
    current: Any = payload
    for segment in str(path).split("."):
        name = segment.strip()
        if not name:
            continue
        if isinstance(current, dict):
            current = current.get(name)
            continue
        if isinstance(current, list):
            try:
                index = int(name)
            except ValueError:
                return None
            if index < 0 or index >= len(current):
                return None
            current = current[index]
            continue
        return None
    return current


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
