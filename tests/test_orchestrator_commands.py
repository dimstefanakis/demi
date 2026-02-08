from __future__ import annotations

from demi.orchestrator import Orchestrator


def test_extract_testing_mode_command_parses_on_off():
    assert Orchestrator._extract_testing_mode_command(["/testing on"]) is True
    assert Orchestrator._extract_testing_mode_command(["/testing off"]) is False
    assert Orchestrator._extract_testing_mode_command(["testing: enabled"]) is True
    assert Orchestrator._extract_testing_mode_command(["testing=disabled"]) is False


def test_extract_testing_mode_command_returns_none_when_missing():
    assert Orchestrator._extract_testing_mode_command(["/project main"]) is None
    assert Orchestrator._extract_testing_mode_command(["hello world"]) is None
