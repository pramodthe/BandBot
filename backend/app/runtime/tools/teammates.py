"""Hand work to another visible agent in the channel, or (Master only) create a new one."""
from __future__ import annotations

from typing import Annotated

from langchain_core.tools import InjectedToolCallId, tool

from app.persistence.models import AgentProfile, AgentTask, db
from app.tasks.queue import hand_off
from app.tasks.teammates import ask_outside, create_teammate as request_teammate, outside_agent


def teammate_tools(task: AgentTask):
    @tool
    async def ask_teammate(teammate: str, request: str, tool_call_id: Annotated[str, InjectedToolCallId],
                           report_back: bool = True) -> dict:
        """Hand work to a visible teammate agent by name. Posts your request in the channel for everyone to see.

        The teammate runs after your turn ends, with their own role and memory. With report_back (default),
        their answer comes back to you as a new task so you can continue; end your turn with a short status.
        With report_back=False, the teammate answers the humans directly. Give complete context and the
        expected output. Use internal helpers (the task tool) instead for quick private subtasks.
        Outside Band agents in the channel are reached the same way; they answer in the channel.
        """
        with db() as session:
            outside = outside_agent(session, task, teammate)
        if outside:
            return await ask_outside(task, outside, request, tool_call_id)
        return hand_off(task, teammate=teammate, request=request, report_back=report_back, call_id=tool_call_id)

    @tool
    async def create_teammate(name: str, role: str, instructions: str,
                              tool_call_id: Annotated[str, InjectedToolCallId]) -> dict:
        """Create a new visible teammate agent in this channel, with its own Band identity, role, and memory.

        Only when no existing teammate's role fits the work. role is one of: coordinator, researcher, writer,
        coder, critic, custom. instructions describe its focus. Returns its name; then hand off with ask_teammate.
        """
        return await request_teammate(task, name=name, role=role, instructions=instructions, call_id=tool_call_id)

    with db() as session:
        profile = session.get(AgentProfile, task.agent_id)
    return [ask_teammate, create_teammate] if profile and profile.is_master else [ask_teammate]
