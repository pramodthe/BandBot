import asyncio
from types import SimpleNamespace as Obj

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import app.api.http as http
import app.api.people as people
import app.workspace.service as service_module
from app.persistence.models import (AgentProfile, AgentTask, BandPlatformRoom, Message,
                                     PlatformTeammate, UserBandConnection, db)
from app.tasks.queue import next_task, update_task
from app.tasks.worker import process, deliver

@pytest.fixture
def platform_app(svc, monkeypatch, offline_band_platform):
    async def noop(*args): pass
    monkeypatch.setattr(svc, 'startup', noop)
    monkeypatch.setattr(http, 'service', svc)
    monkeypatch.setattr(people, 'service', svc)
    monkeypatch.setattr(service_module, 'service', svc)
    monkeypatch.setattr(service_module, 'configuration', lambda: ('offline-model-key', ['gpt-6-luna'], 'gpt-6-luna'))
    with TestClient(http.app) as client:
        users = []
        for name in ('alpha', 'beta'):
            key = 'band_a_platform-' + name
            result = client.post('/api/auth/signup', json={'username': name, 'display_name': name.title(),
                'password': 'test-password', 'band_api_key': key})
            assert result.status_code == 200, result.text
            users.append((result.json()['user']['id'], {'Authorization': 'Bearer ' + result.json()['token']}, key))
        yield client, users, svc, offline_band_platform


def test_saved_key_drives_signup_room_teammate_worker_delivery_and_inbound(platform_app):
    client, ((owner, headers, key), (_, other, other_key)), svc, network = platform_app
    boot = client.get('/api/bootstrap', headers=headers)
    assert boot.json()['band']['api'] == 'agent' and boot.json()['band']['online']
    assert key not in boot.text
    channel = client.post('/api/channels', headers=headers, json={'name': 'Writing'}).json()
    assert channel['band_room_id']
    assert ('create_room', key, channel['band_room_id']) in network.calls
    created = client.post('/api/agents', headers=headers, json={'name': 'Writer', 'specialty': 'Clear writing',
        'channel_id': channel['id'], 'plugins': []})
    assert created.status_code == 200, created.text
    profile = created.json()
    assert profile['band_auth'] == 'platform' and profile['owner_id'] == owner
    with db() as session:
        assert session.get(PlatformTeammate, profile['id']).user_id == owner
        assert session.get(AgentProfile, profile['id']).band_api_key_enc == ''
        assert session.get(BandPlatformRoom, channel['id']).band_agent_id == 'platform-alpha'
    sent = client.post(f"/api/channels/{channel['id']}/messages", headers=headers, json={'content': '@Writer write a draft.', 'client_id': 'same-send'})
    assert sent.status_code == 200
    assert client.post(f"/api/channels/{channel['id']}/messages", headers=headers, json={'content': '@Writer write a draft.', 'client_id': 'same-send'}).json()['id'] == sent.json()['id']
    task = next_task()
    async def executor(task, saver): return 'A completed draft.'
    asyncio.run(process(task, None, executor=executor))
    with db() as session:
        finished = session.get(AgentTask, task.id)
    assert finished.status == 'completed'
    asyncio.run(deliver(finished))
    asyncio.run(deliver(finished))  # A replay cannot send the Band result twice.
    with db() as session:
        assert session.get(AgentTask, task.id).delivery_status == 'sent'
    events = [c for c in network.calls if c[0] == 'event' and 'A completed draft.' in c[3].content]
    assert len(events) == 1 and events[0][1] == key
    history = client.get(f"/api/channels/{channel['id']}/messages", headers=headers)
    assert 'A completed draft.' in history.text and key not in history.text
    assert client.get(f"/api/channels/{channel['id']}/messages", headers=other).status_code == 404
    # Incoming messages use the actual platform subscription adapter and queue the same worker.
    peer = Obj(id='peer-reviewer', name='Reviewer', handle='reviewer', description='', online=True, is_external=True, type='Agent')
    network.peers.append(peer)
    peers = client.get(f"/api/channels/{channel['id']}/peers", headers=headers)
    assert peers.status_code == 200 and peers.json()[0]['band_id'] == peer.id
    from app.persistence.models import User
    with db() as session:
        user = session.get(User, owner)
    asyncio.run(svc.invite_outside_agent(user, channel['id'], peer.id))
    listener = svc._platform_listeners[owner]
    incoming = Obj(id='native-reply', sender_id=peer.id, sender_name='Reviewer', content='@Writer improve the draft.')
    async def receive():
        await listener.adapter.on_message(incoming, None, [], None, None, is_session_bootstrap=False, room_id=channel['band_room_id'])
        await listener.adapter.on_message(incoming, None, [], None, None, is_session_bootstrap=False, room_id=channel['band_room_id'])
    asyncio.run(receive())
    with db() as session:
        inbound = session.exec(select(AgentTask).where(AgentTask.source_key.endswith('platform-native-reply'))).all()
        assert len(inbound) == 1 and inbound[0].reply_to == peer.id
    native_task = next_task()
    asyncio.run(process(native_task, None, executor=executor))
    with db() as session:
        ready = session.get(AgentTask, native_task.id)
    asyncio.run(deliver(ready))
    replies = [c for c in network.calls if c[0] == 'message' and 'A completed draft.' in c[3].content]
    assert len(replies) == 1 and replies[0][1] == key and replies[0][3].mentions[0].id == peer.id
    assert not any(call[1] == other_key for call in network.calls)


def test_transport_isolation_revocation_and_wrong_room_never_use_another_key(platform_app):
    client, ((owner, a, key), (other, b, other_key)), svc, network = platform_app
    first = client.post('/api/channels', headers=a, json={'name': 'Private alpha'}).json()
    second = client.post('/api/channels', headers=b, json={'name': 'Private beta'}).json()
    assert ('create_room', key, first['band_room_id']) in network.calls
    assert ('create_room', other_key, second['band_room_id']) in network.calls
    message = Obj(id='foreign', sender_id='peer', sender_name='Foreign', content='@Writer do work')
    asyncio.run(svc.platform_message(owner, 'platform-alpha', second['band_room_id'], message))
    with db() as session:
        assert not session.exec(select(Message).where(Message.band_message_id == 'foreign')).first()
    assert client.get(f"/api/channels/{first['id']}/peers", headers=b).status_code == 404
    # Revoke the personal key, including its cached live transport.
    network.rejected.add(key)
    asyncio.run(svc._close_platform(owner))
    failed = client.post('/api/channels', headers=a, json={'name': 'Must not fall back'})
    assert failed.status_code == 409
    assert key not in failed.text
    assert client.post('/api/channels', headers=b, json={'name': 'Beta remains connected'}).status_code == 200
    status = client.get('/api/bootstrap', headers=a).json()['band']
    assert status['api'] == 'agent' and not status['online']


def test_delete_teammate_preserves_platform_connection_and_band_identity(platform_app):
    client, ((owner, headers, key), _), _, network = platform_app
    result = client.post('/api/agents', headers=headers, json={'name': 'Temporary Writer'})
    assert result.status_code == 200, result.text
    assert client.delete(f"/api/agents/{result.json()['id']}", headers=headers).status_code == 200
    with db() as session:
        assert session.get(UserBandConnection, owner)
        assert not session.get(PlatformTeammate, result.json()['id'])
    assert client.get('/api/band/connection', headers=headers).json()['online'] is True


def test_key_rotation_replaces_live_transport_and_worker_uses_current_key(platform_app, monkeypatch):
    import app.band.connections as connections
    from app.band.platform import BandIdentity
    client, ((owner, headers, old_key), _), svc, network = platform_app
    channel = client.post('/api/channels', headers=headers, json={'name': 'Rotate'}).json()
    old_listener = svc._platform_listeners[owner]
    client.post('/api/agents', headers=headers, json={'name': 'Rotated Writer', 'channel_id': channel['id']}).json()
    client.post(f"/api/channels/{channel['id']}/messages", headers=headers, json={'content': '@Rotated Writer write.'})
    task = next_task()
    new_key = 'band_a_rotated-alpha'
    network.aliases[new_key] = 'platform-alpha'
    async def identity(key): return BandIdentity(id='platform-alpha', name='Alpha', handle='alpha')
    monkeypatch.setattr(connections, 'fetch_identity', identity)
    updated = client.put('/api/band/connection', headers=headers, json={'api_key': new_key})
    assert updated.status_code == 200 and updated.json()['online'] is True
    assert old_listener.closed and svc._platform_listeners[owner] is not old_listener
    from app.tasks.worker import complete
    complete(task, 'Result after key rotation.')
    with db() as session:
        ready = session.get(AgentTask, task.id)
    asyncio.run(deliver(ready))
    events = [call for call in network.calls if call[0] == 'event' and 'Result after key rotation.' in call[3].content]
    assert len(events) == 1 and events[0][1] == new_key
    assert old_key not in updated.text and new_key not in updated.text


def test_platform_listener_failure_is_scoped_and_shutdown_stops_sdk(monkeypatch):
    import app.band.platform_listener as listener_module
    failures, stopped, signal_handlers = [], [], []
    class Service:
        async def platform_connection_failed(self, user, identity): failures.append((user, identity))
    class SDK:
        async def run(self, *, install_signal_handlers):
            signal_handlers.append(install_signal_handlers)
            raise ValueError('SDK disconnected')
        async def stop(self): stopped.append(True)
    monkeypatch.setattr(listener_module.Agent, 'create', lambda **kwargs: SDK())
    async def scenario():
        listener = listener_module.PlatformListener(Service(), 8, 'identity-8', 'unused-key')
        await listener.task
        await listener.close()
    asyncio.run(scenario())
    assert failures == [(8, 'identity-8')] and stopped == [True]
    assert signal_handlers == [False]


def test_report_back_and_interim_answers_stay_local_until_final_result(platform_app):
    from app.persistence.models import TaskHandoff
    from app.tasks.queue import hand_off
    from app.tasks.worker import complete

    client, ((_, headers, key), _), _, network = platform_app
    channel = client.post('/api/channels', headers=headers, json={'name': 'Team writing'}).json()
    for name in ('Lead Writer', 'Fact Checker', 'Copy Editor'):
        created = client.post('/api/agents', headers=headers,
                              json={'name': name, 'channel_id': channel['id']})
        assert created.status_code == 200, created.text
    client.post(f"/api/channels/{channel['id']}/messages", headers=headers,
                json={'content': '@Lead Writer prepare the final draft.'})
    root = next_task()
    for name in ('Fact Checker', 'Copy Editor'):
        assert hand_off(root, teammate=name, request='Review the draft.', report_back=True,
                        call_id=name)['status'] == 'handed_off'
    complete(root, 'Waiting for the reviews.')

    def finish(task, result):
        complete(task, result)
        with db() as session:
            ready = session.get(AgentTask, task.id)
        asyncio.run(deliver(ready))
        with db() as session:
            return session.get(AgentTask, task.id).delivery_status

    first = next_task()
    assert finish(first, 'Internal fact-check notes.') == 'local_only'
    with db() as session:
        interim = session.exec(select(AgentTask).join(TaskHandoff, TaskHandoff.task_id == AgentTask.id)
            .where(TaskHandoff.root_task_id == root.id, TaskHandoff.kind == 'interim')).one()
    update_task(interim.id, status='running')
    assert finish(interim, 'Internal waiting status.') == 'local_only'
    second = next_task()
    assert finish(second, 'Internal editing notes.') == 'local_only'

    final = next_task()
    assert finish(final, 'The final reviewed draft.') == 'sent'
    outbound = [call for call in network.calls if call[0] in ('event', 'message')]
    assert not any('Internal' in call[3].content for call in outbound)
    assert sum('The final reviewed draft.' in call[3].content for call in outbound) == 1
    assert all(call[1] == key for call in outbound)
