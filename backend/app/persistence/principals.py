"""Snapshot task authority atomically, without loading runtime or authentication code."""
import re
from sqlalchemy.orm import Session as SASession
from sqlalchemy import event
from app.persistence.models import AgentTask, Message, Schedule, TaskHandoff, TaskPrincipal


@event.listens_for(SASession, "before_flush")
def snapshot_principals(session, *_args):
    pending = {item.task_id: item for item in session.new if isinstance(item, TaskPrincipal)}
    handoffs = {item.task_id: item for item in session.new if isinstance(item, TaskHandoff)}
    for task in [item for item in session.new if isinstance(item, AgentTask)]:
        if task.id in pending or session.get(TaskPrincipal, task.id):
            continue
        user_id = None
        match = re.search(r":app-message-(\d+)$", task.source_key)
        if match:
            message = session.get(Message, int(match[1]))
            if message and message.channel_id == task.channel_id and message.author_type == "human":
                user_id = message.author_id
        elif match := re.fullmatch(r"provider-restart:[^:]+:(\d+):.+", task.source_key):
            user_id = int(match[1])  # The authenticated restart endpoint writes this value.
        parent_id = None
        if link := handoffs.get(task.id):
            parent_id = link.root_task_id
        elif match := re.search(r":schedule-(\d+)-", task.source_key):
            schedule = session.get(Schedule, int(match[1]))
            parent_id = schedule.source_key.split(":", 1)[0] if schedule else None
        if parent_id:
            principal = pending.get(parent_id) or session.get(TaskPrincipal, parent_id)
            user_id = principal.user_id if principal else None
        principal = TaskPrincipal(task_id=task.id, user_id=user_id)
        pending[task.id] = principal
        session.add(principal)
