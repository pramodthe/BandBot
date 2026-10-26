"""Offline Agent API boundary shared by regression and browser tests."""
from types import SimpleNamespace as Obj
from uuid import uuid4
from datetime import datetime, timezone
from app.band.platform_listener import PlatformAdapter


class OfflineBandNetwork:
    def __init__(self):
        self.rooms, self.calls, self.listeners = {}, [], []
        self.peers = []
        self.rejected = set()
        self.aliases = {}
        self.managed_agents = {}

    def management_client(self, *, api_key, base_url):
        assert api_key.startswith('band_u_'), 'Agent management selected a non-user credential'
        owner = api_key.removeprefix('band_u_')
        network = self
        async def close(): pass
        async def profile(**kwargs):
            if api_key in network.rejected:
                raise ValueError('Credential revoked')
            network.calls.append(('management_identity', api_key, None))
            return Obj(data=Obj(id=owner, first_name=owner.title(), last_name='', handle=owner))
        async def register(*, agent, request_options):
            if api_key in network.rejected:
                raise ValueError('Credential revoked')
            assert request_options['max_retries'] == 0
            identity = 'managed-' + owner + '-' + uuid4().hex
            key = 'band_a_' + identity
            network.managed_agents[identity] = owner
            network.calls.append(('register', api_key, identity, agent.name))
            return Obj(data=Obj(agent=Obj(id=identity), credentials=Obj(api_key=key)))
        async def delete(identity, **kwargs):
            assert network.managed_agents[identity] == owner
            network.calls.append(('delete', api_key, identity))
            del network.managed_agents[identity]
        return Obj(human_api_profile=Obj(get_my_profile=profile),
            human_api_agents=Obj(register_my_agent=register, delete_my_agent=delete),
            _client_wrapper=Obj(httpx_client=Obj(httpx_client=Obj(aclose=close))))

    def client(self, *, api_key, base_url):
        network = self
        identity = self.aliases.get(api_key, api_key.removeprefix('band_a_'))
        if not api_key.startswith('band_a_'):
            raise AssertionError('Agent API transport selected a non-agent credential')
        async def close(): pass
        def check(room):
            if api_key in network.rejected:
                raise ValueError('Credential revoked')
            known = network.rooms.get(room)
            if known and identity not in known['members']:
                raise PermissionError('Foreign room')
            # The browser API and worker have separate fake-provider processes.
            if not known:
                if not room.startswith('fake-room-' + identity + '-'):
                    raise PermissionError('Foreign room')
                network.rooms[room] = {'members': {identity}, 'messages': [], 'owner': identity}
            return network.rooms[room]
        async def me(**kwargs):
            if api_key in network.rejected:
                raise ValueError('Credential revoked')
            network.calls.append(('identity', api_key, None))
            return Obj(data=Obj(id=identity, name='Band connection', handle=identity, owner_uuid='human-' + identity))
        async def create(*, chat, **kwargs):
            room = 'fake-room-' + identity + '-' + uuid4().hex
            network.rooms[room] = {'members': {identity}, 'messages': [], 'owner': identity}
            network.calls.append(('create_room', api_key, room))
            return Obj(data=Obj(id=room))
        async def participants(room, **kwargs):
            return Obj(data=[Obj(id=member) for member in check(room)['members']])
        async def add(room, *, participant, **kwargs):
            check(room)['members'].add(participant.participant_id)
            network.calls.append(('add', api_key, room, participant.participant_id))
            return Obj(data=Obj(id=participant.participant_id))
        async def remove(room, member, **kwargs):
            check(room)['members'].discard(member)
            network.calls.append(('remove', api_key, room, member))
        async def peers(*, not_in_chat, **kwargs):
            members = check(not_in_chat)['members']
            network.calls.append(('peers', api_key, not_in_chat))
            return Obj(data=[p for p in network.peers if p.id not in members])
        async def message(room, *, message, **kwargs):
            data = check(room)
            assert message.mentions and all(m.id != identity and m.id in data['members'] for m in message.mentions)
            item = Obj(id=uuid4().hex, content=message.content, sender_id=identity, sender_name='Band connection', inserted_at=datetime.now(timezone.utc))
            data['messages'].append(item)
            network.calls.append(('message', api_key, room, message))
            return Obj(data=item)
        async def event(room, *, event, **kwargs):
            check(room)
            network.calls.append(('event', api_key, room, event))
            return Obj(data=Obj(id=uuid4().hex))
        async def context(room, **kwargs): return Obj(data=check(room)['messages'])
        return Obj(agent_api_identity=Obj(get_agent_me=me), agent_api_chats=Obj(create_agent_chat=create),
            agent_api_participants=Obj(list_agent_chat_participants=participants, add_agent_chat_participant=add, remove_agent_chat_participant=remove),
            agent_api_peers=Obj(list_agent_peers=peers), agent_api_messages=Obj(create_agent_chat_message=message),
            agent_api_events=Obj(create_agent_chat_event=event), agent_api_context=Obj(get_agent_chat_context=context),
            _client_wrapper=Obj(httpx_client=Obj(httpx_client=Obj(aclose=close))))

    def listener(self, service, user_id, identity, key):
        class Listener:
            closed = False
            adapter = PlatformAdapter(service, user_id, identity)
            async def close(self): self.closed = True
        listener = Listener()
        self.listeners.append(listener)
        return listener
