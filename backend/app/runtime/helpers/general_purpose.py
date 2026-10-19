"""Focused analysis using this channel's context."""
from __future__ import annotations

from langchain.agents.middleware import ModelCallLimitMiddleware


def general_purpose(by_name: dict) -> dict:
    return {
        "name": "general-purpose",
        "description": "Analyze a focused subtask using scoped channel context.",
        "system_prompt": "Complete the assigned subtask. State evidence and limitations; return a concise report.",
        "middleware": [ModelCallLimitMiddleware(run_limit=12, exit_behavior="error")],
        "tools": [by_name[n] for n in ("channel_history", "recall_memory")],
    }
