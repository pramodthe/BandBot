"""Pause a task until a person in the channel approves an external action."""
from __future__ import annotations

from langchain_core.tools import tool
from langgraph.types import interrupt

from app.persistence.models import AgentTask


def approval_tools(task: AgentTask):
    del task

    @tool
    def request_approval(action: str, details: str = "") -> dict:
        """Pause and ask the humans to approve an action with external effects, before doing it.

        Required before submitting forms, sending or posting messages, purchases or payments, changing or
        deleting data in accounts, or anything hard to undo. Reading and browsing need no approval.
        Describe exactly what you will do and where. The task resumes with the humans' decision.
        """
        if not action.strip():
            return {"error": "Describe the action that needs approval."}
        decision = interrupt({"action": action.strip()[:500], "details": details.strip()[:4000]})
        result = {"approved": bool(decision.get("approved")), "decided_by": decision.get("decided_by", ""),
                  "note": decision.get("note", "")}
        if not result["approved"]:
            result["instruction"] = "Do not perform this action. Tell the humans what you did instead."
        return result

    return [request_approval]
