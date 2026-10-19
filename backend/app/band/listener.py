from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from band import Agent
from band.core.simple_adapter import SimpleAdapter
from band.client.rest import DEFAULT_REQUEST_OPTIONS, AsyncRestClient, aclose_rest_client

from app.auth import decrypt_secret
from app.config import settings
from app.persistence.models import AgentProfile, AgentTask, Channel, Message, db
from sqlmodel import select
from app.tasks import display_names, enqueue
from app.tasks.teammates import link_outside_reply, outside_reply

logger = logging.getLogger(__name__)

class AgentSink(Protocol):
    async def agent_message(
        self,
        profile_id: int,
        room_id: str,
        content: str,
        mentions: list[str],
        band_message_id: str | None,
    ) -> None: ...

    async def agent_typing(self, profile_id: int, room_id: str, active: bool) -> None: ...

    async def agent_status(self, profile_id: int, state: str, error: str | None) -> None: ...


def describe_error(exc: BaseException) -> str:
    """Band REST errors stringify with full response headers; keep just the message."""
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else getattr(body, "error", None)
    message = error.get("message") if isinstance(error, dict) else getattr(error, "message", None)
    status = getattr(exc, "status_code", None)
    if message:
        return f"{message} (HTTP {status})" if status else str(message)
    if status:
        return f"Band returned HTTP {status}"
    return str(exc) or type(exc).__name__


class QueuedAgentAdapter(SimpleAdapter):
    """Acknowledge Band turns after durable enqueue; the worker owns all reasoning."""

    def __init__(self, *, profile_id: int, reply_to: str):
        super().__init__()
        self.profile_id = profile_id
        self.reply_to = reply_to

    @property
    def judges_turns(self) -> bool:
        return False

    async def on_message(self, msg, tools, history, participants_msg, contacts_msg, *,
                         is_session_bootstrap: bool, room_id: str) -> None:
        with db() as session:
            channel = session.exec(select(Channel).where(Channel.band_room_id == room_id)).first()
        if channel is None:
            logger.warning("Ignoring agent turn for unknown room %s", room_id)
            return
        # App-originated messages already committed their jobs before relay. Keep native
        # Band mentions while suppressing the transport echo, even if its receipt is racing.
        if msg.sender_id == self.reply_to:
            with db() as session:
                recent = session.exec(select(Message).where(Message.channel_id == channel.id,
                    Message.author_type == "human").order_by(Message.id.desc()).limit(100)).all()
                for local in recent:
                    if local.band_message_id == msg.id or msg.content == f"**{local.author_name}:** {local.content}":
                        key = f"{self.profile_id}:{room_id}:app-message-{local.id}"
                        if session.exec(select(AgentTask.id).where(AgentTask.source_key == key)).first():
                            return
        # Raw history is kept bounded and treated as untrusted context by the harness.
        context = "\n".join(
            f"{getattr(item, 'sender_name', 'participant')}: {getattr(item, 'content', '')}"
            for item in (history or [])[-30:]
        )[-24000:]
        with db() as session:
            context, prompt = display_names(session, context), display_names(session, msg.content)
            prompt, origin = outside_reply(session, self.profile_id, channel.id, msg.sender_id, prompt)
        task = enqueue(agent_id=self.profile_id, channel_id=channel.id, room_id=room_id,
                       source_id=msg.id, prompt=prompt, context=context,
                       reply_to=self.reply_to or msg.sender_id)
        if origin:
            link_outside_reply(task, origin)


@dataclass
class _Running:
    agent: Agent
    task: asyncio.Task


class AgentManager:
    def __init__(self, sink: AgentSink) -> None:
        self._sink = sink
        self._running: dict[int, _Running] = {}
        self._band_ids: dict[int, str] = {}
        self.status: dict[int, dict[str, str | None]] = {}

    @property
    def local_band_ids(self) -> set[str]:
        return set(self._band_ids.values())

    async def _set_status(self, profile_id: int, state: str, error: str | None = None) -> None:
        self.status[profile_id] = {"state": state, "error": error}
        await self._sink.agent_status(profile_id, state, error)

    async def start(
        self,
        profile: AgentProfile,
        *,
        owner_id: str = "",
    ) -> None:
        assert profile.id is not None
        await self.stop(profile.id)
        adapter = QueuedAgentAdapter(profile_id=profile.id, reply_to=owner_id)
        agent = Agent.create(
            adapter=adapter,
            agent_id=profile.band_agent_id,
            api_key=decrypt_secret(profile.band_api_key_enc),
            ws_url=settings.band_ws_url,
            rest_url=settings.band_rest_url,
        )
        self._band_ids[profile.id] = profile.band_agent_id
        task = asyncio.create_task(self._run(profile.id, agent), name=f"agent-{profile.id}")
        self._running[profile.id] = _Running(agent=agent, task=task)

    async def _run(self, profile_id: int, agent: Agent) -> None:
        await self._set_status(profile_id, "starting")
        try:
            await agent.start()
            await self._set_status(profile_id, "online")
            await agent.run_forever(install_signal_handlers=False)
            await self._set_status(profile_id, "stopped")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Agent %s crashed", profile_id)
            await self._set_status(profile_id, "error", describe_error(exc))
        finally:
            try:
                await agent.stop(timeout=5)
            except Exception:
                logger.exception("Agent %s did not stop cleanly", profile_id)

    async def stop(self, profile_id: int) -> None:
        running = self._running.pop(profile_id, None)
        self._band_ids.pop(profile_id, None)
        if running is None:
            return
        try:
            await running.agent.stop(timeout=5)
        except Exception:
            logger.exception("Agent %s did not stop cleanly", profile_id)
        running.task.cancel()
        await asyncio.gather(running.task, return_exceptions=True)
        await self._set_status(profile_id, "stopped")

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop(pid) for pid in list(self._running)))


@dataclass
class BandIdentity:
    id: str
    name: str
    handle: str


async def fetch_identity(api_key: str) -> BandIdentity:
    client = AsyncRestClient(api_key=api_key, base_url=settings.band_rest_url)
    try:
        me = (await client.agent_api_identity.get_agent_me(request_options=DEFAULT_REQUEST_OPTIONS)).data
        return BandIdentity(id=me.id, name=me.name, handle=me.handle or "")
    finally:
        await aclose_rest_client(client)
