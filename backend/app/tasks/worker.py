"""Run separately: uv run python -m app.worker (one worker per checkpoint file, several tasks at once)."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from band.client.rest import AsyncRestClient, ChatMessageRequest, ChatMessageRequestMentionsItem, aclose_rest_client
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from deepagents.backends.utils import file_data_to_string
from sqlmodel import select
from sqlalchemy import delete, update

from app.auth import decrypt_secret
from app.config import settings
from app import documents
from app.openai_provider import LegacyCheckpoint, migrate_profiles, pin_task
from app.runtime.harness import build_harness
from app.runtime.tools.mcp import integration_tools
from app.computer import TaskComputer
from app.persistence.models import (AgentProfile, AgentTask, Message, MessageComponent, MessageDraft, db, init_db,
                                    utcnow)
from app.tasks.drafts import Draft, answer_text, clear_draft
from app.tasks.schedules import fire_due
from app.tasks.queue import (approval_decisions, await_approval, is_interim, next_task, queue_report, recover_tasks,
                             report_target, stop_for_budget, update_task)
from app.tasks.usage import BudgetExceeded, UsageRecorder, over_budget
from app.workspace.audit import record_agent, summarize_args

logger = logging.getLogger(__name__)

# UsageRecorder checks budgets before each model call; this also stops tasks busy in long tool calls.
BUDGET_POLLS = 4

TOOL_LABELS = {
    "web_search": "Searching the web", "fetch_page": "Reading a web page", "run_python": "Running code",
    "sandbox_exec": "Running a command", "computer_screenshot": "Looking at the computer screen",
    "computer_open_browser": "Opening a browser", "computer_action": "Using the computer",
    "sandbox_write_file": "Writing a file", "sandbox_export_file": "Exporting a file",
    "channel_history": "Reading the channel", "recall_memory": "Checking memory", "remember": "Saving to memory",
    "write_todos": "Planning", "request_approval": "Asking for approval", "schedule_task": "Scheduling",
    "show_table": "Building a table", "show_chart": "Drawing a chart", "show_spec": "Building a spec sheet",
    "ask_user": "Preparing a question", "present_ui": "Building an interactive reply", "ui_catalog": "Choosing UI components",
    "generate_sandboxed_ui": "Building an interactive interface",
    "generate_image": "Generating an image",
}


class ApprovalRequired(Exception):
    """The graph paused for approval or user input; waiting releases the worker slot."""

    def __init__(self, requests: list[tuple[str, dict]]):
        super().__init__(f"{len(requests)} approval request(s)")
        self.requests = requests


def _requests(snapshot) -> list[tuple[str, dict]]:
    return [(item.id, item.value if isinstance(item.value, dict) else {"action": str(item.value)})
            for item in snapshot.interrupts]


def server_action(message) -> str:
    """Progress label for provider-side tools, which run inside the model call rather than as tool calls."""
    content = getattr(message, "content", "")
    blocks = content if isinstance(content, list) else []
    if any(isinstance(b, dict) and b.get("type") == "code_interpreter_call" for b in blocks):
        return "Running code in a quick sandbox"
    return ""


def record_activity(task: AgentTask, namespace: tuple, message) -> None:
    """Audit a model response's tool calls (helpers' and provider-side ones too); UsageRecorder counts its tokens."""
    if getattr(message, "type", "") != "ai":
        return
    via = {"via": "helper"} if namespace else {}
    for call in getattr(message, "tool_calls", []):
        record_agent(task, "tool_call", ref=call.get("id") or None, tool=call["name"],
                     args=summarize_args(call.get("args")), **via)
    content = getattr(message, "content", "")
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "code_interpreter_call":
            record_agent(task, "tool_call", ref=block.get("id") or None, tool="code_interpreter", **via)


def describe_call(call: dict) -> str:
    if call["name"] == "task":
        return f"Asking the {call['args'].get('subagent_type') or 'general-purpose'} helper"
    if call["name"] == "ask_teammate":
        return f"Handing off to {call['args'].get('teammate') or 'a teammate'}"
    return TOOL_LABELS.get(call["name"], f"Using {call['name']}")


def config_for(task: AgentTask) -> dict:
    return {"configurable": {"thread_id": task.id}, "recursion_limit": settings.task_recursion_limit}


async def execute(task: AgentTask, checkpointer, *, factory=build_harness, computer: TaskComputer | None = None) -> str:
    # Browser tests answer without a model provider. A real key disables the shortcut.
    if os.getenv("E2E_FAKE_AGENT") == "1" and not settings.openai_api_key and not settings.band_user_api_key:
        return "I can help with that."
    from app.providers.context import task_services
    computer = computer or TaskComputer(task)
    async with task_services(task, computer), computer:
        return await _execute_on_computer(task, checkpointer, factory=factory, computer=computer)


async def _execute_on_computer(task: AgentTask, checkpointer, *, factory, computer) -> str:
    with db() as session:
        profile = session.get(AgentProfile, task.agent_id)
        # Report-backs and interim statuses are agent chatter; only answers for people stream.
        draft = Draft(task, enabled=report_target(session, task) is None and not is_interim(session, task))
    if profile is None:
        raise ValueError("Agent was deleted before this task could finish")
    from app.providers.bindings import bindings_for
    identity = bindings_for(task).get("_agent", {}).get("band_id")
    if identity and identity != profile.band_agent_id:
        from app.providers.contracts import ProviderError
        raise ProviderError("llm", "agent_identity", detail="This task belongs to a removed teammate. Its saved progress cannot run under another teammate's identity.")
    if factory is build_harness:
        from app.agent_connections import pin_runtime, runtime_for
        from app.providers.bindings import bindings_for
        binding = bindings_for(task).get("llm", {})
        runtime = runtime_for(profile.id)
        if binding and not binding.get("legacy"):
            if binding.get("unresolved"):
                from app.providers.language_models import for_binding
                for_binding(binding)
            profile.model = binding.get("model") or profile.model
        else:
            profile.model = pin_runtime(task, profile, runtime) if runtime else pin_task(task, profile)
    clear_draft(task.id)
    extra_tools = await integration_tools(profile.name, agent_id=profile.id)
    graph = factory(task, profile, checkpointer, computer=computer, extra_tools=extra_tools)
    config = {**config_for(task), "callbacks": [UsageRecorder(task, profile.model)]}
    snapshot = await graph.aget_state(config)
    if snapshot.values and not snapshot.next:
        final = snapshot.values
    else:
        attached = documents.task_documents(task)
        manifest = "\n".join(f"{d.name}: document_id={d.id}, status={d.status}" for d in attached)
        inputs = None if snapshot.values else {"messages": [{"role": "user", "content":
            f"Current time: {utcnow():%Y-%m-%d %H:%M} UTC\n\nRecent room context:\n{task.context}\n\n"
            f"Request:\n{task.prompt}\n\nAttached documents:\n{manifest or 'None; list_documents can find saved files.'}"}]}
        if requests := _requests(snapshot):
            decisions = approval_decisions([approval_id for approval_id, _ in requests])
            if decisions is None:
                raise ApprovalRequired(requests)
            inputs = Command(resume=decisions)
        action, step = "", ""
        async for namespace, mode, data in graph.astream(inputs, config, stream_mode=["updates", "messages"],
                                                         subgraphs=True, durability="sync"):
            if mode == "messages":
                draft.feed(namespace, *data)
                continue
            draft.flush()
            previous = (action, step)
            for _node, value in data.items():
                if not isinstance(value, dict):
                    continue
                if todos := value.get("todos"):
                    current = next((i for i, t in enumerate(todos) if t["status"] == "in_progress"), None)
                    step = "" if current is None else f"Step {current + 1}/{len(todos)}: {todos[current]['content']}"
                for message in value.get("messages", []):
                    record_activity(task, namespace, message)
                    action = server_action(message) or action
                    for call in getattr(message, "tool_calls", []):
                        action = describe_call(call)
                    if getattr(message, "type", "") == "tool":
                        action = "Reviewing results"
            if (action, step) != previous:
                update_task(task.id, progress=" · ".join(filter(None, (action, step)))[:1000])
        snapshot = await graph.aget_state(config)
        if requests := _requests(snapshot):
            raise ApprovalRequired(requests)
        final = snapshot.values
    with db() as session:
        exports = session.get(AgentTask, task.id).artifacts
    combined = {a["path"]: a["content"] for a in exports}
    for path, data in final.get("files", {}).items():
        if not isinstance(data, dict) or "content" not in data:
            continue
        combined.setdefault(path, file_data_to_string(data))
    artifacts = []
    remaining = 200000
    for path, content in combined.items():
        content = content[:remaining]
        artifacts.append({"path": path, "content": content})
        remaining -= len(content)
        if remaining <= 0 or len(artifacts) >= 20:
            break
    update_task(task.id, artifacts=artifacts)
    for message in reversed(final.get("messages", [])):
        if getattr(message, "type", "") == "ai" and not getattr(message, "tool_calls", []):
            text = answer_text(message)
            if text.strip():
                return documents.finalize_document_links(task, text[:60000])
    raise ValueError("Agent finished without a final answer")


def complete(task: AgentTask, result: str) -> None:
    # Result and local message are committed together. Replaying completion cannot duplicate a reply.
    with db() as session:
        claimed = session.exec(update(AgentTask).where(
            AgentTask.id == task.id, AgentTask.status == "running", AgentTask.cancel_requested == False
        ).values(result=result, status="completed", progress="Completed", updated_at=utcnow()))
        if claimed.rowcount:
            profile = session.get(AgentProfile, task.agent_id)
            if profile is None:
                raise ValueError("Agent was deleted")
            key = f"task:{task.id}"
            if not session.exec(select(Message).where(Message.band_message_id == key)).first():
                # A report-back hand-off answers the requesting agent, which then continues from it.
                target = report_target(session, task)
                mentions = [{"type": "agent", "id": target[1].id, "name": target[1].name}] if target else []
                message = Message(channel_id=task.channel_id, author_type="agent", author_id=task.agent_id,
                                  author_name=profile.name, content=result, band_message_id=key,
                                  to_humans=target is None and not is_interim(session, task), mentions=mentions)
                session.add(message)
                session.flush()
                session.exec(update(AgentTask).where(AgentTask.id == task.id).values(reply_message_id=message.id))
                session.exec(update(MessageComponent).where(MessageComponent.task_id == task.id,
                             MessageComponent.message_id == None).values(message_id=message.id))  # noqa: E711
                if target:
                    queue_report(session, task, target[0], profile.name, result)
        else:
            session.exec(update(AgentTask).where(AgentTask.id == task.id, AgentTask.status == "running",
                         AgentTask.cancel_requested == True).values(status="cancelled", progress="Cancelled"))
        session.exec(delete(MessageDraft).where(MessageDraft.task_id == task.id))
        session.commit()


async def deliver(task: AgentTask) -> None:
    """At-most-once send attempt; ambiguous sends require human review, never blind retry."""
    if not task.reply_to:
        update_task(task.id, delivery_status="local_only")
        return
    with db() as session:
        profile = session.get(AgentProfile, task.agent_id)
    if profile is None:
        update_task(task.id, delivery_status="agent_deleted")
        return
    with db() as session:
        claimed = session.exec(update(AgentTask).where(AgentTask.id == task.id,
            AgentTask.status == "completed", AgentTask.delivery_status == "pending")
            .values(delivery_status="sending", updated_at=utcnow()))
        session.commit()
        if not claimed.rowcount:
            return
    client = None
    try:
        client = AsyncRestClient(api_key=decrypt_secret(profile.band_api_key_enc), base_url=settings.band_rest_url)
        response = await client.agent_api_messages.create_agent_chat_message(
            task.room_id, message=ChatMessageRequest(content=task.result[:8000], mentions=[
                ChatMessageRequestMentionsItem(id=task.reply_to)]), request_options={"max_retries": 0, "timeout_in_seconds": 30})
        with db() as session:
            message = session.exec(select(Message).where(Message.band_message_id == f"task:{task.id}")).first()
            if message:
                message.band_message_id = response.data.id
                session.add(message)
                session.commit()
        update_task(task.id, delivery_status="sent")
    except Exception:
        logger.exception("Band delivery outcome uncertain for task %s; local answer retained", task.id)
        update_task(task.id, delivery_status="unknown")
    finally:
        if client is not None:
            await aclose_rest_client(client)


async def process(task: AgentTask, saver, *, executor=execute) -> None:
    runner = asyncio.create_task(executor(task, saver))
    try:
        async with asyncio.timeout(settings.task_timeout):
            polls = 0
            while not runner.done():
                with db() as session:
                    current = session.get(AgentTask, task.id)
                    spent = over_budget(session, task.agent_id, task.channel_id) if polls % BUDGET_POLLS == 0 else None
                polls += 1
                if current.cancel_requested or spent:
                    runner.cancel()
                    await asyncio.gather(runner, return_exceptions=True)
                    clear_draft(task.id)
                    if current.cancel_requested:
                        update_task(task.id, status="cancelled", progress="Cancelled")
                    else:
                        stop_for_budget(task, spent)
                    return
                await asyncio.wait({runner}, timeout=0.5)
            result = await runner
            complete(task, result)
    except ApprovalRequired as paused:
        clear_draft(task.id)
        await_approval(task, paused.requests)
    except BudgetExceeded as spent:
        clear_draft(task.id)
        stop_for_budget(task, spent.reason)
    except LegacyCheckpoint:
        from app.providers.contracts import ProviderError
        from app.providers.errors import block
        block(task, ProviderError("llm", "legacy_checkpoint", detail="The original provider binding is ambiguous. Start a linked new task; saved progress is preserved."))
        clear_draft(task.id)
        update_task(task.id, status="failed", progress="Needs connection",
                    error="The original provider binding is ambiguous. Saved progress is preserved; explicitly start a linked new task with current providers.")
    except asyncio.CancelledError:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        # Leave running state for restart recovery; never turn shutdown into task failure.
        raise
    except Exception as exc:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        clear_draft(task.id)
        from app.providers.errors import classify, block
        if provider_error := classify(exc, task):
            block(task, provider_error)
            update_task(task.id, status="failed", progress="Needs connection", error=provider_error.detail)
            return
        # Keep raw provider errors out of the UI (they can include credentials/headers).
        logger.exception("Task %s failed", task.id)
        update_task(task.id, status="failed", progress="Task failed; retry to resume saved progress",
                    error="Time limit reached" if isinstance(exc, TimeoutError) else type(exc).__name__)


def _lock_exclusively(lock) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("A worker already owns this checkpoint file")


async def work() -> None:
    # A supervisor's SIGTERM gets the same cancellation/cleanup path as Ctrl+C.
    # Windows event loops don't support signal handlers; Ctrl+C still works there.
    if sys.platform != "win32":
        current = asyncio.current_task()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, current.cancel)
    init_db()
    migrate_profiles()
    path = Path(settings.checkpoint_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".worker.lock", "w") as lock:
        _lock_exclusively(lock)
        recover_tasks()
        documents.recover_jobs()
        clear_draft()
        # A crash between send and receipt cannot safely be retried.
        with db() as session:
            for task in session.exec(select(AgentTask).where(AgentTask.delivery_status == "sending")).all():
                task.delivery_status = "unknown"
                task.updated_at = utcnow()
                session.add(task)
            session.commit()
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            await saver.setup()
            logger.info("Deep Agents worker ready (up to %d concurrent tasks)", settings.worker_concurrency)
            await run_queue(saver, concurrency=settings.worker_concurrency)


async def run_queue(saver, *, concurrency: int, executor=execute, stop_when_idle: bool = False) -> None:
    """Run up to `concurrency` tasks and deliveries at once; next_task keeps agents ordered per channel.

    On shutdown, running tasks are cancelled and left `running` for recovery, as with a single task.
    """
    running: dict[str, asyncio.Task] = {}
    delivering: dict[str, asyncio.Task] = {}
    document_job = None
    provider_job = None
    from app.providers.verification import process_one as verify_one
    from app.providers.retrieval import process_change
    document_poll_at = 0.0
    try:
        while True:
            if provider_job is None or provider_job.done():
                if provider_job and not provider_job.cancelled() and provider_job.exception():
                    logger.error("Provider job failed (%s)", type(provider_job.exception()).__name__)
                async def service_jobs():
                    await verify_one()
                    await process_change()
                provider_job = asyncio.create_task(service_jobs(), name="provider-services")
            if (document_job is None or document_job.done()) and asyncio.get_running_loop().time() >= document_poll_at:
                if document_job and not document_job.cancelled() and document_job.exception():
                    logger.error("Document processing failed (%s)", type(document_job.exception()).__name__)
                document_job = asyncio.create_task(documents.process_one(), name="document-processing")
                document_poll_at = asyncio.get_running_loop().time() + 1
            try:
                from app.workspace.health import beat
                beat("busy" if running else "idle")
            except Exception:
                logger.exception("Could not record the worker heartbeat")
            try:
                fire_due()
            except Exception:
                logger.exception("Could not queue scheduled tasks")
            while len(running) < concurrency and (task := next_task()):
                running[task.id] = asyncio.create_task(process(task, saver, executor=executor),
                                                       name=f"task-{task.id}")
            with db() as session:
                pending = session.exec(select(AgentTask).where(
                    AgentTask.status == "completed", AgentTask.delivery_status == "pending")).all()
            for item in pending:
                if item.id not in delivering:
                    delivering[item.id] = asyncio.create_task(deliver(item), name=f"deliver-{item.id}")
            active = [*running.values(), *delivering.values()]
            if document_job and not document_job.done() and (not stop_when_idle or active):
                active.append(document_job)
            if not active:
                if stop_when_idle:
                    return
                await asyncio.sleep(0.5)
                continue
            await asyncio.wait(active, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
            for group in (running, delivering):
                for key, job in list(group.items()):
                    if job.done():
                        del group[key]
                        if not job.cancelled() and job.exception():
                            logger.error("Worker job %s crashed", key, exc_info=job.exception())
    finally:
        jobs = [*running.values(), *delivering.values(), *([document_job] if document_job else []), *([provider_job] if provider_job else [])]
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(work())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
