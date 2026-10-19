"""Schedule timing, validation, replay-safe creation, and firing, without a clock dependency."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.auth import issue_token
from app.main import app
from app.persistence.models import AgentTask, Schedule, db
from app.tasks.schedules import MAX_SCHEDULES_PER_CHANNEL, ScheduleError, create_schedule, fire_due, next_run
from test_tasks import job  # noqa: F401  (fixture)

UTC = timezone.utc


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@pytest.mark.parametrize("schedule,after,expected", [
    (Schedule(cadence="daily", time_of_day="09:00", timezone="UTC"), "2026-10-16 08:00", "2026-10-16 09:00"),
    (Schedule(cadence="daily", time_of_day="09:00", timezone="UTC"), "2026-10-16 09:00", "2026-10-17 09:00"),
    # Friday after 9am: the next weekday is Monday.
    (Schedule(cadence="weekdays", time_of_day="09:00", timezone="UTC"), "2026-10-16 10:00", "2026-10-19 09:00"),
    # Friday after the weekly Friday slot: one week later.
    (Schedule(cadence="weekly", weekday=4, time_of_day="09:00", timezone="UTC"), "2026-10-16 10:00", "2026-10-23 09:00"),
    (Schedule(cadence="interval", interval_minutes=30), "2026-10-16 10:00", "2026-10-16 10:30"),
    # New York leaves daylight saving on 2026-11-01: 9am local moves from 13:00 to 14:00 UTC.
    (Schedule(cadence="daily", time_of_day="09:00", timezone="America/New_York"), "2026-10-31 14:00", "2026-11-01 14:00"),
    (Schedule(cadence="daily", time_of_day="09:00", timezone="America/New_York"), "2026-10-30 12:00", "2026-10-30 13:00"),
    (Schedule(cadence="once", run_date="2026-10-20", time_of_day="18:30", timezone="UTC"), "2026-10-16 00:00", "2026-10-20 18:30"),
])
def test_next_run(schedule, after, expected):
    assert next_run(schedule, at(after)) == at(expected)


def test_one_off_in_the_past_has_no_next_run():
    assert next_run(Schedule(cadence="once", run_date="2026-01-01", time_of_day="09:00", timezone="UTC"),
                    at("2026-10-16 00:00")) is None


@pytest.mark.parametrize("kwargs,message", [
    ({"request": " ", "cadence": "daily", "time_of_day": "09:00"}, "1-4000"),
    ({"request": "x", "cadence": "hourly"}, "cadences"),
    ({"request": "x", "cadence": "daily", "time_of_day": "9am"}, "HH:MM"),
    ({"request": "x", "cadence": "daily", "time_of_day": "09:00", "tz": "Mars/Base"}, "Unknown timezone"),
    ({"request": "x", "cadence": "weekly", "time_of_day": "09:00", "weekday": "funday"}, "weekday"),
    ({"request": "x", "cadence": "interval", "every_minutes": 5}, "at least 15"),
    ({"request": "x", "cadence": "once", "time_of_day": "09:00", "run_date": "soon"}, "YYYY-MM-DD"),
    ({"request": "x", "cadence": "once", "time_of_day": "09:00", "run_date": "2001-01-01"}, "already passed"),
])
def test_invalid_schedules_are_explained(job, kwargs, message):
    task, _, _ = job
    with pytest.raises(ScheduleError, match=message):
        create_schedule(task, call_id="bad", **kwargs)


def test_tool_creates_schedule_once_and_limits_per_channel(job):
    from app.runtime.tools import tools_for
    task, _, _ = job
    tool = next(t for t in tools_for(task) if t.name == "schedule_task")
    call = {"type": "tool_call", "name": "schedule_task", "id": "sched-1", "args": {
        "request": "Summarize AI news", "cadence": "weekdays", "time_of_day": "09:00",
        "timezone": "America/Los_Angeles"}}
    first = tool.invoke(call)
    assert '"status": "scheduled"' in first.content and "every weekday at 09:00 (America/Los_Angeles)" in first.content
    tool.invoke(call)  # A replayed tool call must not create a second schedule.
    with db() as session:
        rows = session.exec(select(Schedule).where(Schedule.channel_id == task.channel_id)).all()
    assert len(rows) == 1 and rows[0].agent_id == task.agent_id and rows[0].room_id == task.room_id
    for n in range(MAX_SCHEDULES_PER_CHANNEL - 1):
        create_schedule(task, request="More", cadence="interval", every_minutes=60, call_id=f"fill-{n}")
    with pytest.raises(ScheduleError, match="already has"):
        create_schedule(task, request="Too many", cadence="interval", every_minutes=60, call_id="over")


def test_fire_due_queues_once_advances_and_skips_missed_runs(job):
    task, _, _ = job
    created = create_schedule(task, request="Post the standup reminder", cadence="daily", time_of_day="09:00",
                              tz="UTC", call_id="daily")
    with db() as session:
        schedule = session.get(Schedule, created["id"])
        schedule.next_run_at = at("2026-10-10 09:00")  # Missed several days while the worker was off.
        session.add(schedule)
        session.commit()
    now = at("2026-10-16 12:00")
    assert fire_due(now) >= 1
    fire_due(now)  # Nothing new is due; a repeat must not duplicate the run.
    with db() as session:
        runs = session.exec(select(AgentTask).where(AgentTask.source_key.like(f"%:schedule-{created['id']}-%"))).all()
        schedule = session.get(Schedule, created["id"])
    assert len(runs) == 1 and "Post the standup reminder" in runs[0].prompt
    assert runs[0].channel_id == task.channel_id and runs[0].status == "queued"
    assert schedule.next_run_at.replace(tzinfo=UTC) == at("2026-10-17 09:00") and schedule.enabled


def test_one_off_disables_after_firing(job):
    task, _, _ = job
    created = create_schedule(task, request="Remind us", cadence="once", time_of_day="09:00",
                              run_date="2099-01-01", tz="UTC", call_id="once")
    fire_due(at("2099-01-01 09:01"))
    with db() as session:
        assert not session.get(Schedule, created["id"]).enabled


def test_schedule_api_lists_and_cancels_for_members(job):
    task, _, user = job
    created = create_schedule(task, request="Weekly report", cadence="weekly", weekday="monday",
                              time_of_day="08:00", tz="UTC", call_id="api")
    headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
    with TestClient(app) as client:
        listed = client.get(f"/api/channels/{task.channel_id}/schedules", headers=headers).json()
        assert [row["when"] for row in listed if row["id"] == created["id"]] == ["every Monday at 08:00 (UTC)"]
        url = f"/api/channels/{task.channel_id}/schedules/{created['id']}"
        assert client.delete(url, headers=headers).status_code == 200
        assert client.delete(url, headers=headers).status_code == 404
        assert all(row["id"] != created["id"] for row in
                   client.get(f"/api/channels/{task.channel_id}/schedules", headers=headers).json())
