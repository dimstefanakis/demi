from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_github_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent tests from creating real GitHub repos via .env credentials."""
    for key in (
        "GITHUB_ORG",
        "GITHUB_APP_ID",
        "GITHUB_APP_CLIENT_ID",
        "GITHUB_APP_INSTALLATION_ID",
        "GITHUB_APP_PRIVATE_KEY",
    ):
        monkeypatch.setenv(key, "")
