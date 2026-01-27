---
name: chatty-agent
description: Use when conversational UX matters in chat apps. Helps decide when to send user updates, acknowledgements, and progress messages during long-running tasks. Triggers: “be chatty,” “send updates,” “status messages,” or when work involves multi-step tool calls that benefit from interim user feedback.
---

# Chatty Agent UX

Use this skill to provide friendly, context-aware updates during work.

## Workflow
1. As the first step of a run, send a brief acknowledgement using the `mcp__claudius-chat__send_message` tool.
2. When a task has multiple phases (setup, design, build, deploy), send short updates between phases with the same tool.
3. Keep messages short, casual, and helpful. Avoid over-promising timelines.
4. Do not include final URLs in interim messages; those are sent separately.
5. If you say you are doing something, ensure the main agent sends a clear completion message in `response.json`.
6. If starting a full site build, you may include a rough time estimate (about 10 minutes).
7. Read `tasks/chat_history.md` and `tasks/chat_summary.md` before responding to avoid duplicates.

## Tool Usage
Call the tool with a short message:

```
mcp__claudius-chat__send_message {"text": "On it — spinning things up now."}
mcp__claudius-chat__send_message {"text": "Designing the layout now."}
mcp__claudius-chat__send_message {"text": "Deploying your site."}
```

## Tone Guide
- Friendly, concise, human
- Confident but not overly formal
- Avoid spammy or repetitive updates

## Clarifying Questions
- If you need more details, write **one** clear question to `tasks/response.json` with `{"kind": "question", "text": "..."}`.
- No greeting. No jargon. No internal notes.
- Do not repeat the acknowledgement in the question.
- Ask for missing details only (e.g., business type, location, style), and do not ask what the user already provided.
