from __future__ import annotations

import json
from pathlib import Path

from demi.config import Settings
from demi.runtime import tenant_tooling


def test_bootstrap_tooling_no_packages_skips_install(monkeypatch, tmp_path):
    calls: list[dict[str, object]] = []

    def _fake_install(*, tooling_root: Path, frozen: bool) -> None:
        calls.append({"tooling_root": tooling_root, "frozen": frozen})

    monkeypatch.setattr(tenant_tooling, "_run_bun_install", _fake_install)

    settings = Settings(
        root_dir=tmp_path,
        tenant_tooling_enabled=True,
        tenant_tooling_packages="",
    )
    tenant_root = tmp_path / "tenant-a"
    result = tenant_tooling.bootstrap_tenant_tooling(tenant_root=tenant_root, settings=settings)

    assert calls == []
    assert result.packages == {}
    assert (tenant_root / "tooling" / "package.json").exists()
    assert (tenant_root / "tooling.lock").exists()


def test_bootstrap_tooling_installs_once_then_reuses_lock(monkeypatch, tmp_path):
    call_count = 0

    def _fake_install(*, tooling_root: Path, frozen: bool) -> None:
        nonlocal call_count
        call_count += 1
        (tooling_root / "bun.lock").write_text("# lock\n", encoding="utf-8")
        bin_dir = tooling_root / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "supabase").write_text("", encoding="utf-8")

    monkeypatch.setattr(tenant_tooling, "_run_bun_install", _fake_install)

    settings = Settings(
        root_dir=tmp_path,
        tenant_tooling_enabled=True,
        tenant_tooling_packages='{"supabase":"1.200.3"}',
    )
    tenant_root = tmp_path / "tenant-b"

    first = tenant_tooling.bootstrap_tenant_tooling(tenant_root=tenant_root, settings=settings)
    first_lock_payload = json.loads((tenant_root / "tooling.lock").read_text(encoding="utf-8"))
    second = tenant_tooling.bootstrap_tenant_tooling(tenant_root=tenant_root, settings=settings)
    lock_payload = json.loads((tenant_root / "tooling.lock").read_text(encoding="utf-8"))

    assert first.installed is True
    assert second.installed is False
    assert call_count == 1
    assert first.bin_path is not None
    assert first_lock_payload["generated_at"] == lock_payload["generated_at"]
    assert lock_payload["packages"] == {"supabase": "1.200.3"}
    assert lock_payload["checksums"]["package_json"]
