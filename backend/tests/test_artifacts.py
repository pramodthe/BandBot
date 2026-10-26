from uuid import uuid4

from fastapi.testclient import TestClient

from app.auth import issue_token
from app.main import app
from app.persistence.models import AgentTask, ChannelMember, DirectMessage, Message, User, db
from app.tasks import update_task
from app.tasks.worker import complete
from app.workspace.service import serialize_messages



def export_reply(task):
    artifacts = [{"path": "/home/oai/share/app.html", "content": "<h1>Saved app 🐴</h1>"},
                 {"path": "/draft.txt", "content": "Notes"}]
    update_task(task.id, status="running", artifacts=artifacts)
    complete(task, "Built the app.")
    with db() as session:
        saved = session.get(AgentTask, task.id)
        # Confirm the attachment survives delivery replacing the local Band key.
        message = session.get(Message, saved.reply_message_id)
        message.band_message_id = "delivered-" + uuid4().hex
        session.add(message)
        session.commit()
        return saved.reply_message_id, artifacts


def test_export_metadata_attaches_to_its_durable_reply_without_inlining_source(job):
    task, _, user = job
    message_id, artifacts = export_reply(task)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {issue_token(user.id)}"}
        response = client.get(f"/api/channels/{task.channel_id}/messages", headers=headers)
        assert response.status_code == 200
        reply = next(message for message in response.json() if message["id"] == message_id)
        assert reply["artifacts"] == [{"task_id": task.id, "path": item["path"],
                                       "size": len(item["content"].encode("utf-8"))} for item in artifacts]
        assert "<h1>" not in response.text
        detail = client.get(f"/api/tasks/{task.id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["artifacts"] == artifacts
    with db() as session:
        unrelated = Message(channel_id=task.channel_id, author_type="human", author_id=user.id,
                            author_name=user.display_name, content="Another message")
        session.add(unrelated)
        session.commit()
        assert serialize_messages(session, [unrelated])[0]["artifacts"] == []


def test_task_artifact_source_requires_channel_membership(job):
    task, _, owner = job
    export_reply(task)
    with db() as session:
        outsider = User(username=uuid4().hex, display_name="Other user", password_hash="unused")
        session.add(outsider)
        session.commit()
        outsider_id = outsider.id
    with TestClient(app) as client:
        assert client.get(f"/api/tasks/{task.id}").status_code == 401
        headers = {"Authorization": f"Bearer {issue_token(outsider_id)}"}
        assert client.get(f"/api/tasks/{task.id}", headers=headers).status_code == 403
        with db() as session:
            session.add(ChannelMember(channel_id=task.channel_id, member_type="human", member_id=outsider_id))
            session.add(DirectMessage(channel_id=task.channel_id, user_id=owner.id, agent_id=task.agent_id))
            session.commit()
        assert client.get(f"/api/tasks/{task.id}", headers=headers).status_code == 404


def test_cross_channel_task_reply_link_cannot_expose_artifact_metadata(job):
    task, _, user = job
    export_reply(task)
    with db() as session:
        other = Message(channel_id=task.channel_id + 10000, author_type="human", author_id=user.id,
                        author_name=user.display_name, content="Unrelated channel")
        session.add(other)
        session.flush()
        stored = session.get(AgentTask, task.id)
        stored.reply_message_id = other.id
        session.add(stored)
        session.commit()
        assert serialize_messages(session, [other])[0]["artifacts"] == []
