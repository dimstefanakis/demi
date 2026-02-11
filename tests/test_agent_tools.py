import json
import asyncio

import pytest
from demi.agent.claude import ClaudeAgent, _sdk_message_log_data
from demi.agent.inflight import InflightTextStream
from demi.config import Settings


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
    assert "mcp__demi-chat__unregister_scheduler_trigger" in agent.allowed_tools
    assert "mcp__demi-chat__record_deploy" in agent.allowed_tools
    assert "mcp__demi-chat__record_domain_quote" in agent.allowed_tools
    assert "mcp__demi-unsplash__search_photos" in agent.allowed_tools


def test_default_setting_sources():
    agent = ClaudeAgent()
    assert agent.setting_sources == ["project", "local"]


def test_default_subagents_configured():
    agent = ClaudeAgent()
    assert "interaction-helper" in agent.agents
    assert "planner" in agent.agents
    assert "product-designer" in agent.agents
    assert "software-engineer" in agent.agents
    assert "devops-engineer" in agent.agents
    assert "reviewer" in agent.agents
    tools = agent.agents["interaction-helper"].tools or []
    assert "mcp__demi-chat__send_payment_link" in tools
    assert "Skill" in tools

    designer_tools = agent.agents["product-designer"].tools or []
    assert "Skill" in designer_tools
    assert "Bash" in designer_tools
    assert "mcp__demi-unsplash__search_photos" in designer_tools

    software_tools = agent.agents["software-engineer"].tools or []
    assert "Skill" in software_tools
    assert "Bash" in software_tools
    assert "mcp__demi-supabase__provision_managed_backend" in software_tools

    devops_tools = agent.agents["devops-engineer"].tools or []
    assert "Skill" in devops_tools
    assert "Bash" in devops_tools
    assert "mcp__demi-github__prepare_repo" in devops_tools
    assert "mcp__demi-chat__record_deploy" in devops_tools


def test_subagent_model_defaults():
    agent = ClaudeAgent()
    assert agent.agents["product-designer"].model == "sonnet"
    assert agent.agents["software-engineer"].model == "sonnet"
    assert agent.agents["devops-engineer"].model == "sonnet"
    assert agent.agents["reviewer"].model == "opus"


def test_subagents_do_not_have_task_tool():
    agent = ClaudeAgent()
    for name, definition in agent.agents.items():
        tools = definition.tools or []
        assert "Task" not in tools, f"subagent {name} should not include Task in its tools"


def test_execution_prompt_explicitly_requires_specialized_subagents():
    settings = Settings()
    prompt = settings.claude_prompt_path.read_text(encoding="utf-8").lower()
    assert "use the planner agent" in prompt
    assert "use the reviewer agent" in prompt
    assert "product-designer" in prompt
    assert "software-engineer" in prompt
    assert "devops-engineer" in prompt


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


@pytest.mark.asyncio
async def test_interaction_prompt_stream_stops_when_close_event_set():
    stream = InflightTextStream(queue=asyncio.Queue())
    close_event = asyncio.Event()
    prompt_stream = ClaudeAgent._interaction_prompt_stream(
        "ROUTING MODE",
        inflight_stream=stream,
        close_event=close_event,
    )

    first = await prompt_stream.__anext__()
    assert first["type"] == "user"

    close_event.set()
    with pytest.raises(StopAsyncIteration):
        await prompt_stream.__anext__()
    assert stream.accepting is False
