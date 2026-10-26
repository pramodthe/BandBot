"""Band turns enqueue once without running a model in the API process."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4
from sqlmodel import select
from app.band.listener import QueuedAgentAdapter
from app.persistence.models import AgentTask, Channel, db, init_db


def test_bridge_persists_and_deduplicates_turns():
    init_db()
    room = uuid4().hex
    with db() as session:
        session.add(Channel(name=room, band_room_id=room))
        session.commit()
    adapter = QueuedAgentAdapter(profile_id=7, reply_to="owner")
    msg = SimpleNamespace(id="source-1", sender_id="owner", content="Research this")
    async def scenario():
        for _ in range(2):
            await adapter.on_message(msg, None, [], None, None,
                                     is_session_bootstrap=True, room_id=room)
    asyncio.run(scenario())
    with db() as session:
        tasks = session.exec(select(AgentTask).where(AgentTask.room_id == room)).all()
    assert len(tasks) == 1
    assert tasks[0].status == "queued"
    assert tasks[0].prompt == "Research this"
    assert tasks[0].reply_to == "owner"
