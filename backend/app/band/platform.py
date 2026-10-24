"""Band transport authenticated by a person's saved Agent API credential."""
from datetime import datetime
import asyncio

from band.client.rest import (AsyncRestClient, ChatMessageRequest, ChatMessageRequestMentionsItem,
                              ParticipantRequest, aclose_rest_client)
from band_rest.types import ChatRoomRequest, ChatEventRequest

from app.band.org import BandOrg, Owner, NO_RETRY
from app.config import settings


class AgentPlatformOrg(BandOrg):
    api = "agent"

    def __init__(self, key: str, *, user_id: int, identity: str):
        super().__init__(key)
        self.user_id, self.identity = user_id, identity

    def status(self):
        return {**super().status(), "personal": True, "api": "agent"}

    @property
    def online(self):
        return super().online and self.error is None

    async def connect(self):
        rest = AsyncRestClient(api_key=self._api_key, base_url=settings.band_rest_url)
        try:
            async with asyncio.timeout(25):
                me = (await rest.agent_api_identity.get_agent_me(request_options=NO_RETRY)).data
            if me.id != self.identity:
                raise ValueError("Connection identity changed")
        except Exception:
            await aclose_rest_client(rest)
            self.error = "Your Band connection is unavailable. Check or update your Agent API key in People."
            return
        self.owner = Owner(id=me.id, name=me.name, handle=me.handle or "")
        self._rest, self.error = rest, None

    async def register_agent(self, name, description):
        raise ValueError("Agent API connections create app teammates; registering Band identities requires Enterprise access.")

    async def delete_agent(self, agent_id):
        raise ValueError("The Agent API cannot delete a Band identity")

    async def create_room(self, title):
        response = await self._client().agent_api_chats.create_agent_chat(
            chat=ChatRoomRequest(title=title), request_options=NO_RETRY)
        self._room_members[response.data.id] = {self.identity}
        return response.data.id

    async def _members(self, room_id):
        if room_id not in self._room_members:
            response = await self._client().agent_api_participants.list_agent_chat_participants(room_id, request_options=NO_RETRY)
            self._room_members[room_id] = {participant.id for participant in response.data}
        return self._room_members[room_id]

    async def add_participant(self, room_id, band_id):
        if band_id in await self._members(room_id):
            return
        await self._client().agent_api_participants.add_agent_chat_participant(room_id,
            participant=ParticipantRequest(participant_id=band_id, role="member"), request_options=NO_RETRY)
        self._room_members[room_id].add(band_id)

    async def remove_participant(self, room_id, band_id):
        if band_id == self.identity:
            raise ValueError("The connection identity must remain in its room")
        await self._client().agent_api_participants.remove_agent_chat_participant(room_id, band_id, request_options=NO_RETRY)
        self.forget_participant(room_id, band_id)

    async def agent_peers(self, room_id):
        response = await self._client().agent_api_peers.list_agent_peers(not_in_chat=room_id, page_size=100, request_options=NO_RETRY)
        return [peer for peer in response.data if str(getattr(peer, "type", "Agent")).lower() == "agent"]

    async def send(self, room_id, content, mentions):
        recipients = [(identity, handle) for identity, handle in mentions if identity != self.identity and not identity.startswith("app-")]
        if recipients:
            response = await self._client().agent_api_messages.create_agent_chat_message(room_id,
                message=ChatMessageRequest(content=content, mentions=[ChatMessageRequestMentionsItem(id=i,
                    **({"handle": handle} if handle else {})) for i, handle in recipients]), request_options=NO_RETRY)
        else:
            # Agent API text messages need another native participant. App-only activity
            # is recorded as an event, without inventing a human or mentioning ourselves.
            response = await self._client().agent_api_events.create_agent_chat_event(room_id,
                event=ChatEventRequest(content=content, message_type="task", metadata={"source": "band_bots"}), request_options=NO_RETRY)
        return response.data.id

    async def messages_since(self, room_id, since: datetime | None):
        response = await self._client().agent_api_context.get_agent_chat_context(room_id, limit=100, request_options=NO_RETRY)
        return sorted([message for message in response.data if since is None or message.inserted_at is None or message.inserted_at >= since],
                      key=lambda message: message.inserted_at.timestamp() if message.inserted_at else 0)
