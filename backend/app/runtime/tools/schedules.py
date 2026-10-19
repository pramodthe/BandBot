"""Schedule this agent to run again in the current channel."""
from __future__ import annotations

from typing import Annotated, Literal

from langchain_core.tools import InjectedToolCallId, tool

from app.persistence.models import AgentTask
from app.tasks.schedules import (ScheduleError, cancel_schedule as cancel_channel_schedule, channel_schedules,
                                 create_schedule, describe, serialize_schedule)
from app.workspace.audit import record_agent


def schedule_tools(task: AgentTask):
    @tool
    def schedule_task(request: str, cadence: Literal["once", "daily", "weekdays", "weekly", "interval"],
                      tool_call_id: Annotated[str, InjectedToolCallId], time_of_day: str = "", weekday: str = "",
                      date: str = "", every_minutes: int = 0, timezone: str = "") -> dict:
        """Schedule yourself to perform a request later in this channel, once or repeatedly.

        time_of_day is 24-hour HH:MM in `timezone` (an IANA name such as America/New_York; empty uses the
        workspace default). weekly needs weekday (monday-sunday); once needs date (YYYY-MM-DD); interval
        needs every_minutes (at least 15). Write `request` as the complete instruction to carry out at run
        time. Each run starts a new task whose answer is posted here. Confirm the schedule to the humans.
        """
        try:
            return create_schedule(task, request=request, cadence=cadence, time_of_day=time_of_day,
                                   weekday=weekday, run_date=date, every_minutes=every_minutes, tz=timezone,
                                   call_id=tool_call_id)
        except ScheduleError as exc:
            return {"error": str(exc)}

    @tool
    def list_schedules() -> list[dict]:
        """List this channel's active schedules with their ids, cadence, and next run time (UTC)."""
        return [serialize_schedule(s) for s in channel_schedules(task.channel_id)]

    @tool
    def cancel_schedule(schedule_id: int) -> dict:
        """Cancel one of this channel's schedules by id (see list_schedules)."""
        cancelled = cancel_channel_schedule(task.channel_id, schedule_id)
        if cancelled:
            record_agent(task, "schedule_cancelled", schedule=describe(cancelled), prompt=cancelled.prompt)
        return {"status": "cancelled", **serialize_schedule(cancelled)} if cancelled else {
            "error": "No active schedule with that id in this channel."}

    return [schedule_task, list_schedules, cancel_schedule]
