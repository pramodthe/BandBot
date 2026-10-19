"""Review a deliverable against the request."""
from __future__ import annotations

from langchain.agents.middleware import ModelCallLimitMiddleware


def critic(by_name: dict) -> dict:
    return {
        "name": "critic",
        "description": "Review evidence, code, and proposed deliverables for errors and gaps.",
        "system_prompt": "Check the supplied deliverable against the request. Return concrete issues and verification limits.",
        "middleware": [ModelCallLimitMiddleware(run_limit=12, exit_behavior="error")],
        "tools": [by_name[n] for n in ("code_interpreter", "run_python", "recall_memory") if n in by_name],
    }
