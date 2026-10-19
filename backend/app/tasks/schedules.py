"""Scheduled agent requests: validated at creation, fired by the worker, which holds the queue lock."""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.config import settings
from app.persistence.models import AgentProfile, AgentTask, Schedule, db, utcnow
from app.tasks.queue import channel_context, enqueue
from app.workspace.audit import record_agent

logger = logging.getLogger(__name__)

CADENCES = ("once", "daily", "weekdays", "weekly", "interval")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MAX_SCHEDULES_PER_CHANNEL = 20
MIN_INTERVAL_MINUTES = 15


class ScheduleError(ValueError):
    """A message safe to return to the agent."""


def _aware(value: datetime) -> datetime:
    # SQLite returns naive datetimes; schedule times are always stored in UTC.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def next_run(schedule: Schedule, after: datetime) -> datetime | None:
    """The first run strictly after `after`, in UTC, or None once a one-off has passed."""
    after = _aware(after)
    if schedule.cadence == "interval":
        return after + timedelta(minutes=schedule.interval_minutes)
    zone = ZoneInfo(schedule.timezone)
    hour, minute = map(int, schedule.time_of_day.split(":"))
    if schedule.cadence == "once":
        at = datetime.combine(date.fromisoformat(schedule.run_date), time(hour, minute), zone).astimezone(timezone.utc)
        return at if at > after else None
    local_day = after.astimezone(zone).date()
    for offset in range(8):  # A weekly slot earlier today recurs within 7 days.
        day = local_day + timedelta(days=offset)
        if (schedule.cadence == "weekdays" and day.weekday() >= 5) or (
                schedule.cadence == "weekly" and day.weekday() != schedule.weekday):
            continue
        at = datetime.combine(day, time(hour, minute), zone).astimezone(timezone.utc)
        if at > after:
            return at
    raise AssertionError("A daily, weekday, or weekly schedule always recurs within 8 days")


def describe(schedule: Schedule) -> str:
    if schedule.cadence == "interval":
        return f"every {schedule.interval_minutes} minutes"
    when = {"once": f"on {schedule.run_date}", "daily": "every day", "weekdays": "every weekday",
            "weekly": f"every {WEEKDAYS[schedule.weekday or 0].title()}"}[schedule.cadence]
    return f"{when} at {schedule.time_of_day} ({schedule.timezone})"


def serialize_schedule(schedule: Schedule) -> dict:
    return {"id": schedule.id, "channel_id": schedule.channel_id, "agent_id": schedule.agent_id,
            "prompt": schedule.prompt, "when": describe(schedule), "created_by": schedule.created_by,
            "next_run_at": _aware(schedule.next_run_at).isoformat() if schedule.next_run_at else None,
            "last_run_at": _aware(schedule.last_run_at).isoformat() if schedule.last_run_at else None}


def create_schedule(task: AgentTask, *, request: str, cadence: str, time_of_day: str = "", weekday: str = "",
                    run_date: str = "", every_minutes: int = 0, tz: str = "", call_id: str) -> dict:
    """Validate and store a schedule for the task's agent and channel. Replays return the original."""
    request = request.strip()
    if not 1 <= len(request) <= 4000:
        raise ScheduleError("Provide a request of 1-4000 characters to perform at run time.")
    if cadence not in CADENCES:
        raise ScheduleError(f"Use one of these cadences: {', '.join(CADENCES)}.")
    tz = tz.strip() or settings.default_timezone
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError(f"Unknown timezone {tz!r}. Use an IANA name such as America/New_York.") from None
    schedule = Schedule(source_key=f"{task.id}:{call_id}", channel_id=task.channel_id, agent_id=task.agent_id,
                        room_id=task.room_id, reply_to=task.reply_to, prompt=request, cadence=cadence, timezone=tz)
    if cadence == "interval":
        if every_minutes < MIN_INTERVAL_MINUTES:
            raise ScheduleError(f"Intervals must be at least {MIN_INTERVAL_MINUTES} minutes.")
        schedule.interval_minutes = every_minutes
    else:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_of_day.strip()):
            raise ScheduleError("Give time_of_day as 24-hour HH:MM, for example 09:00.")
        schedule.time_of_day = time_of_day.strip()
    if cadence == "weekly":
        if weekday.strip().lower() not in WEEKDAYS:
            raise ScheduleError("Weekly schedules need a weekday such as monday.")
        schedule.weekday = WEEKDAYS.index(weekday.strip().lower())
    if cadence == "once":
        try:
            schedule.run_date = date.fromisoformat(run_date.strip()).isoformat()
        except ValueError:
            raise ScheduleError("One-off schedules need a date as YYYY-MM-DD.") from None
    schedule.next_run_at = next_run(schedule, utcnow())
    if schedule.next_run_at is None:
        raise ScheduleError("That time has already passed. Choose a future date and time.")
    with db() as session:
        existing = session.exec(select(Schedule).where(Schedule.source_key == schedule.source_key)).first()
        if existing:
            return {"status": "scheduled", **serialize_schedule(existing)}
        active = session.exec(select(Schedule.id).where(Schedule.channel_id == task.channel_id,
                                                        Schedule.enabled == True)).all()  # noqa: E712
        if len(active) >= MAX_SCHEDULES_PER_CHANNEL:
            raise ScheduleError(f"This channel already has {MAX_SCHEDULES_PER_CHANNEL} schedules. Cancel one first.")
        agent = session.get(AgentProfile, task.agent_id)
        schedule.created_by = agent.name if agent else ""
        session.add(schedule)
        record_agent(task, "schedule_created", session=session, ref=f"schedule:{call_id}",
                     schedule=describe(schedule), prompt=request)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.exec(select(Schedule).where(Schedule.source_key == schedule.source_key)).one()
            return {"status": "scheduled", **serialize_schedule(existing)}
        session.refresh(schedule)
        return {"status": "scheduled", **serialize_schedule(schedule)}


def channel_schedules(channel_id: int) -> list[Schedule]:
    with db() as session:
        return session.exec(select(Schedule).where(Schedule.channel_id == channel_id, Schedule.enabled == True)  # noqa: E712
                            .order_by(Schedule.next_run_at)).all()


def cancel_schedule(channel_id: int, schedule_id: int) -> Schedule | None:
    with db() as session:
        schedule = session.get(Schedule, schedule_id)
        if schedule is None or schedule.channel_id != channel_id or not schedule.enabled:
            return None
        schedule.enabled = False
        session.add(schedule)
        session.commit()
        session.refresh(schedule)
        return schedule


def fire_due(now: datetime | None = None) -> int:
    """Queue every due run. Run ids make a crash between queueing and advancing safe to repeat.

    Runs missed while the worker was down fire once; the next run is computed from now (no backfill).
    """
    now = now or utcnow()
    with db() as session:
        due = session.exec(select(Schedule).where(Schedule.enabled == True,  # noqa: E712
                                                  Schedule.next_run_at <= now)).all()
    for schedule in due:
        run_at = _aware(schedule.next_run_at)
        with db() as session:
            agent = session.get(AgentProfile, schedule.agent_id)
            context = channel_context(session, schedule.channel_id) if agent else ""
        if agent:
            enqueue(agent_id=schedule.agent_id, channel_id=schedule.channel_id, room_id=schedule.room_id,
                    source_id=f"schedule-{schedule.id}-{run_at.isoformat()}", context=context,
                    prompt=f"Scheduled request ({describe(schedule)}), set up earlier in this channel:\n{schedule.prompt}",
                    reply_to=schedule.reply_to)
        following = next_run(schedule, max(_aware(now), run_at)) if agent else None
        with db() as session:
            stored = session.get(Schedule, schedule.id)
            stored.last_run_at = now
            stored.next_run_at = following
            stored.enabled = following is not None
            session.add(stored)
            session.commit()
    return len(due)
