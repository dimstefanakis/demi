from __future__ import annotations

from types import SimpleNamespace
import sys

import pytest

from demi.runtime.docker_pool import DockerPool, DockerPoolConfig


@pytest.mark.asyncio
async def test_allocate_workspace_uses_stable_tenant_path(tmp_path, monkeypatch):
    pool = DockerPool(
        DockerPoolConfig(
            image="demi-agent:local",
            pool_root=tmp_path / "pool",
            pool_size=0,
        )
    )

    async def _scan_existing_noop() -> None:
        return None

    async def _start_idle_unexpected():
        raise AssertionError("allocate_workspace should not start pool containers for tenant paths")

    monkeypatch.setattr(pool, "_scan_existing", _scan_existing_noop)
    monkeypatch.setattr(pool, "_start_idle_container", _start_idle_unexpected)

    tenant = SimpleNamespace(id=42, key="telegram:6418644860", workspace_path=None)

    first = await pool.allocate_workspace(tenant)
    second = await pool.allocate_workspace(tenant)

    expected = (tmp_path / "pool" / "tenant-42").resolve()
    assert first == expected
    assert second == expected
    assert expected.exists()


@pytest.mark.asyncio
async def test_allocate_workspace_preserves_existing_pool_path(tmp_path, monkeypatch):
    pool = DockerPool(
        DockerPoolConfig(
            image="demi-agent:local",
            pool_root=tmp_path / "pool",
            pool_size=0,
        )
    )
    legacy_path = (tmp_path / "pool" / "slot-legacy").resolve()

    async def _scan_existing_noop() -> None:
        return None

    async def _start_idle_unexpected():
        raise AssertionError("allocate_workspace should not start pool containers for tenant paths")

    monkeypatch.setattr(pool, "_scan_existing", _scan_existing_noop)
    monkeypatch.setattr(pool, "_start_idle_container", _start_idle_unexpected)

    tenant = SimpleNamespace(
        id=42,
        key="telegram:6418644860",
        workspace_path=str(legacy_path),
    )

    workspace = await pool.allocate_workspace(tenant)
    assert workspace == legacy_path
    assert legacy_path.exists()


@pytest.mark.asyncio
async def test_ensure_pool_skips_when_pool_size_zero(tmp_path, monkeypatch):
    pool = DockerPool(
        DockerPoolConfig(
            image="demi-agent:local",
            pool_root=tmp_path / "pool",
            pool_size=0,
        )
    )

    called = False

    async def _scan_existing_mark_called() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(pool, "_scan_existing", _scan_existing_mark_called)
    await pool.ensure_pool()

    assert called is False


@pytest.mark.asyncio
async def test_run_cmd_times_out_by_default(monkeypatch):
    monkeypatch.setenv("DOCKER_COMMAND_TIMEOUT_SECONDS", "0.1")
    with pytest.raises(RuntimeError, match=r"command timed out after 0\.1s:"):
        await DockerPool._run_cmd([sys.executable, "-c", "import time; time.sleep(0.3)"])


@pytest.mark.asyncio
async def test_run_cmd_timeout_override_disables_timeout(monkeypatch):
    monkeypatch.setenv("DOCKER_COMMAND_TIMEOUT_SECONDS", "0.1")
    out = await DockerPool._run_cmd(
        [sys.executable, "-c", "import time; time.sleep(0.2); print('ok')"],
        timeout_seconds=0,
    )
    assert out == "ok"
