from claudius.agent.claude import ClaudeAgent


def test_default_allowed_tools():
    agent = ClaudeAgent()
    assert "Bash" in agent.allowed_tools
    assert "WebSearch" in agent.allowed_tools
    assert "WebFetch" in agent.allowed_tools
    assert "AskUserQuestion" in agent.allowed_tools
    assert "Task" in agent.allowed_tools
    assert "Skill" in agent.allowed_tools
    assert "mcp__claudius-chat__send_message" in agent.allowed_tools
    assert "mcp__claudius-chat__record_deploy" in agent.allowed_tools
    assert "mcp__claudius-chat__record_domain_quote" in agent.allowed_tools
    assert "mcp__claudius-unsplash__search_photos" in agent.allowed_tools


def test_default_setting_sources():
    agent = ClaudeAgent()
    assert agent.setting_sources == ["project", "local"]


def test_default_subagents_configured():
    agent = ClaudeAgent()
    assert "interaction-agent" in agent.agents
