"""Code and calculations on the task computer."""
from __future__ import annotations

from langchain.agents.middleware import ModelCallLimitMiddleware


def coder(by_name: dict) -> dict:
    return {
        "name": "coder",
        "description": "Develop code and verify calculations or tests in the isolated Python sandbox.",
        "system_prompt": (
            "Write minimal correct code. Use code_interpreter, when available, for quick self-contained calculations "
            "and data work: it starts instantly but cannot see files on the agent's computer. Use run_python when "
            "the work needs those files, installed packages, or the network. For chart requests, return verified "
            "labelled numeric data, units, coverage, and aggregation for the parent to render with present_ui. "
            "Do not plot or save an image unless the human explicitly asks for an export. "
            "Report execution limits honestly."),
        "middleware": [ModelCallLimitMiddleware(run_limit=12, exit_behavior="error")],
        "tools": [by_name[n] for n in ("code_interpreter", "run_python", "sandbox_exec", "sandbox_write_file",
                                       "sandbox_export_file", "publish_app", "request_approval", "channel_history", "recall_memory")
                  if n in by_name],
    }
