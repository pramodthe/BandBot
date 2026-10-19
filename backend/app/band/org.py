from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from band.client.rest import (
    DEFAULT_REQUEST_OPTIONS,
    AsyncRestClient,
    ChatMessageRequest,
    ChatMessageRequestMentionsItem,
    ParticipantRequest,
    aclose_rest_client,
)
from band_rest.human_api_chats.types.create_my_chat_room_request_chat import CreateMyChatRoomRequestChat
from band_rest.types import ChatMessage
from band_rest.types.agent_register_request import AgentRegisterRequest

from app.band.listener import describe_error
from app.config import settings

logger = logging.getLogger(__name__)

# Registration mints a key that is shown exactly once, so a retried request could
# create an agent whose key we never see.
NO_RETRY = {**DEFAULT_REQUEST_OPTIONS, "max_retries": 0}


@dataclass
class Owner:
    id: str
    name: str
    handle: str


class BandOrg:
    """The org's Band account, driven through the Human API with one user API key."""

    def __init__(self) -> None:
        self.owner: Owner | None = None
        self.error: str | None = None
        self._rest: AsyncRestClient | None = None
        self._room_members: dict[str, set[str]] = {}

    @property
    def configured(self) -> bool:
        return bool(settings.band_user_api_key)

    @property
    def online(self) -> bool:
        return self.owner is not None and self._rest is not None

    def status(self) -> dict:
        return {
            "configured": self.configured,
            "online": self.online,
            "owner_name": self.owner.name if self.owner else None,
            "owner_handle": self.owner.handle if self.owner else None,
            "error": self.error,
        }

    def is_owner_reference(self, value: str) -> bool:
        if not self.owner:
            return False
        value = value.strip().lstrip("@").lower()
        return value in {self.owner.id.lower(), self.owner.handle.lower(), self.owner.name.lower()}

    async def connect(self) -> None:
        if not self.configured:
            self.error = "BAND_USER_API_KEY is not set"
            return
        if settings.band_user_api_key.startswith("band_a_"):
            self.error = (
                "BAND_USER_API_KEY is an agent key (band_a_...). Create a user API key (band_u_...) "
                "at app.band.ai/users/settings"
            )
            logger.error(self.error)
            return
        rest = AsyncRestClient(api_key=settings.band_user_api_key, base_url=settings.band_rest_url)
        try:
            profile = (await rest.human_api_profile.get_my_profile(request_options=DEFAULT_REQUEST_OPTIONS)).data
        except Exception as exc:
            await aclose_rest_client(rest)
            self.error = describe_error(exc)
            logger.error("Band rejected BAND_USER_API_KEY: %s", self.error)
            return
        name = " ".join(p for p in (profile.first_name, profile.last_name) if p) or profile.handle
        self.owner = Owner(id=profile.id, name=name, handle=profile.handle)
        self._rest = rest
        self.error = None

    async def close(self) -> None:
        if self._rest is not None:
            await aclose_rest_client(self._rest)
            self._rest = None

    def _client(self) -> AsyncRestClient:
        if self._rest is None:
            raise RuntimeError(self.error or "Not connected to Band")
        return self._rest

    # --- agents ---

    async def register_agent(self, name: str, description: str) -> tuple[str, str]:
        """Registers an agent owned by the org; returns (agent_id, api_key)."""
        response = await self._client().human_api_agents.register_my_agent(
            agent=AgentRegisterRequest(name=name, description=description), request_options=NO_RETRY
        )
        return response.data.agent.id, response.data.credentials.api_key

    async def delete_agent(self, agent_id: str) -> None:
        await self._client().human_api_agents.delete_my_agent(
            agent_id, force=True, request_options=DEFAULT_REQUEST_OPTIONS
        )

    # --- rooms ---

    async def create_room(self, title: str) -> str:
        response = await self._client().human_api_chats.create_my_chat_room(
            chat=CreateMyChatRoomRequestChat(title=title), request_options=DEFAULT_REQUEST_OPTIONS
        )
        room_id = response.data.id
        self._room_members[room_id] = {self.owner.id} if self.owner else set()
        return room_id

    async def _members(self, room_id: str) -> set[str]:
        if room_id not in self._room_members:
            response = await self._client().human_api_participants.list_my_chat_participants(
                room_id, request_options=DEFAULT_REQUEST_OPTIONS
            )
            self._room_members[room_id] = {p.id for p in response.data}
        return self._room_members[room_id]

    async def add_participant(self, room_id: str, band_id: str) -> None:
        members = await self._members(room_id)
        if band_id in members:
            return
        await self._client().human_api_participants.add_my_chat_participant(
            room_id,
            participant=ParticipantRequest(participant_id=band_id, role="member"),
            request_options=DEFAULT_REQUEST_OPTIONS,
        )
        members.add(band_id)

    async def remove_participant(self, room_id: str, band_id: str) -> None:
        await self._client().human_api_participants.remove_my_chat_participant(
            room_id, band_id, request_options=DEFAULT_REQUEST_OPTIONS
        )
        self._room_members.get(room_id, set()).discard(band_id)

    async def agent_peers(self, room_id: str) -> list:
        """Band agents the org can add to this room (its own, shared, global, and contacts), minus current members."""
        response = await self._client().human_api_peers.list_my_peers(
            not_in_chat=room_id, type="Agent", page_size=100, request_options=DEFAULT_REQUEST_OPTIONS
        )
        return list(response.data)

    def forget_participant(self, room_id: str, band_id: str) -> None:
        self._room_members.get(room_id, set()).discard(band_id)

    def note_participant(self, room_id: str, band_id: str) -> None:
        if room_id in self._room_members:
            self._room_members[room_id].add(band_id)

    # --- messages ---

    async def send(self, room_id: str, content: str, mentions: list[tuple[str, str]]) -> str:
        response = await self._client().human_api_messages.send_my_chat_message(
            room_id,
            message=ChatMessageRequest(
                content=content,
                mentions=[
                    ChatMessageRequestMentionsItem(id=i, **({"handle": h} if h else {})) for i, h in mentions
                ],
            ),
            request_options=DEFAULT_REQUEST_OPTIONS,
        )
        return response.data.id

    async def messages_since(self, room_id: str, since: datetime | None) -> list[ChatMessage]:
        response = await self._client().human_api_messages.list_my_chat_messages(
            room_id,
            message_type="text",
            since=since,
            page_size=50,
            request_options=DEFAULT_REQUEST_OPTIONS,
        )
        return sorted(response.data, key=lambda m: m.inserted_at.timestamp() if m.inserted_at else 0)
