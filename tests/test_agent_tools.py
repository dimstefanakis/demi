import json

from demi.agent.claude import ClaudeAgent, _sdk_message_log_data


def test_default_allowed_tools():
    agent = ClaudeAgent()
    assert "Bash" in agent.allowed_tools
    assert "WebSearch" in agent.allowed_tools
    assert "WebFetch" in agent.allowed_tools
    assert "AskUserQuestion" in agent.allowed_tools
    assert "Task" in agent.allowed_tools
    assert "Skill" in agent.allowed_tools
    assert "mcp__demi-chat__send_message" in agent.allowed_tools
    assert "mcp__demi-chat__send_payment_link" in agent.allowed_tools
    assert "mcp__demi-chat__record_deploy" in agent.allowed_tools
    assert "mcp__demi-chat__record_domain_quote" in agent.allowed_tools
    assert "mcp__demi-unsplash__search_photos" in agent.allowed_tools


def test_default_setting_sources():
    agent = ClaudeAgent()
    assert agent.setting_sources == ["project", "local"]


def test_default_subagents_configured():
    agent = ClaudeAgent()
    assert "interaction-agent" in agent.agents
    tools = agent.agents["interaction-agent"].tools or []
    assert "mcp__demi-chat__send_payment_link" in tools


def test_sdk_message_log_data_does_not_include_repr():
    class FakeSdkMessage:
        subtype = "tool_result"

        def __repr__(self) -> str:
            return "ToolResult(token='ghs_secret_token')"

    payload = _sdk_message_log_data(FakeSdkMessage())

    assert payload["class"] == "FakeSdkMessage"
    assert payload["subtype"] == "tool_result"
    assert "repr" not in payload
    assert "ghs_secret_token" not in json.dumps(payload)
