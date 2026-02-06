import asyncio
import json

import demi.runtime.agent_entrypoint as agent_entrypoint
from demi.agent.inflight import InflightTextStream


def test_load_runtime_env_from_process_collects_github_runtime_keys(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_short_lived")
    monkeypatch.setenv("GITHUB_REPO_FULL_NAME", "acme/site")
    monkeypatch.setenv("GITHUB_REPO_NAME", "site")
    monkeypatch.setenv("GITHUB_REPO_URL", "https://github.com/acme/site")
    monkeypatch.setenv("UNRELATED_SECRET", "should-not-be-collected")

    runtime_env = agent_entrypoint._load_runtime_env_from_process()

    assert runtime_env is not None
    assert runtime_env["GITHUB_TOKEN"] == "ghs_short_lived"
    assert runtime_env["GITHUB_REPO_FULL_NAME"] == "acme/site"
    assert runtime_env["GITHUB_REPO_NAME"] == "site"
    assert runtime_env["GITHUB_REPO_URL"] == "https://github.com/acme/site"
    assert "UNRELATED_SECRET" not in runtime_env


def test_load_runtime_env_from_process_returns_none_when_missing(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPO_FULL_NAME", raising=False)
    monkeypatch.delenv("GITHUB_REPO_NAME", raising=False)

    runtime_env = agent_entrypoint._load_runtime_env_from_process()

    assert runtime_env is None


def test_run_lease_heartbeat_interval_bounds(tmp_path):
    short = agent_entrypoint.RunLeaseHeartbeat(
        db=None,
        run_id=1,
        tasks_dir=tmp_path,
        lease_seconds=4,
        poll_interval=1.0,
    )
    medium = agent_entrypoint.RunLeaseHeartbeat(
        db=None,
        run_id=1,
        tasks_dir=tmp_path,
        lease_seconds=80,
        poll_interval=1.0,
    )
    long = agent_entrypoint.RunLeaseHeartbeat(
        db=None,
        run_id=1,
        tasks_dir=tmp_path,
        lease_seconds=200,
        poll_interval=1.0,
    )
    zero = agent_entrypoint.RunLeaseHeartbeat(
        db=None,
        run_id=1,
        tasks_dir=tmp_path,
        lease_seconds=0,
        poll_interval=1.0,
    )

    assert short._heartbeat_interval() == 5.0
    assert medium._heartbeat_interval() == 20.0
    assert long._heartbeat_interval() == 30.0
    assert zero._heartbeat_interval() == 15.0


def test_pump_inflight_updates_filters_by_run_id_and_does_not_replay(tmp_path):
    updates_path = tmp_path / "inflight_updates.jsonl"
    updates_path.write_text(
        "\n".join(
            [
                json.dumps({"run_id": 10, "text": "old-run-update", "assets": []}),
                json.dumps({"run_id": 11, "text": "current-run-update", "assets": []}),
            ]
        )
        + "\n"
    )

    async def _run_once(target_run_id: int) -> list[str]:
        stream = InflightTextStream(queue=asyncio.Queue())
        stop_event = asyncio.Event()
        task = asyncio.create_task(
            agent_entrypoint._pump_inflight_updates(
                tmp_path,
                stream,
                stop_event,
                db=None,
                run_id=target_run_id,
            )
        )
        await asyncio.sleep(0.25)
        stop_event.set()
        await task
        values: list[str] = []
        while True:
            try:
                values.append(stream.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return values

    first_values = asyncio.run(_run_once(11))
    second_values = asyncio.run(_run_once(11))

    assert first_values == ["current-run-update"]
    assert second_values == []
