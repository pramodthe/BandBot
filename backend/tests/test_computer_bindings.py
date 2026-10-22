"""Provider binding transactions on both supported application databases."""
from uuid import uuid4

from app.persistence.models import (AgentProfile, AgentTask, Channel, ComputerScope,
                                    TaskComputerBinding, bind_computer, db, init_db)


def test_same_scope_in_one_flush_and_stale_startup_backfill(monkeypatch):
    init_db()
    with db() as session:
        agent = AgentProfile(name="Binding test", band_agent_id=uuid4().hex, band_api_key_enc="offline")
        channel = Channel(name=uuid4().hex)
        session.add_all([agent, channel])
        session.commit()
        tasks = [AgentTask(id=uuid4().hex, source_key=uuid4().hex, agent_id=agent.id,
                           channel_id=channel.id, room_id="offline", prompt="Binding test") for _ in range(2)]
        # Both tasks select the same newly created scope before ORM rows are flushed.
        session.add_all(tasks)
        session.commit()
        scope = session.get(ComputerScope, f"agent-{agent.band_agent_id}-channel-{channel.id}")
        assert scope.provider == "e2b"
        bindings = [session.get(TaskComputerBinding, task.id) for task in tasks]
        assert bindings[0].computer_key == bindings[1].computer_key
        expected = bindings[0].computer_key
    with db() as session:
        get = session.get
        # Simulate a worker's stale lookup after the API already backfilled the row.
        monkeypatch.setattr(session, "get", lambda model, key, *a, **kw:
                            None if model is TaskComputerBinding else get(model, key, *a, **kw))
        bind_computer(session, tasks[0], legacy=True)
        session.commit()
    with db() as session:
        assert session.get(TaskComputerBinding, tasks[0].id).computer_key == expected
