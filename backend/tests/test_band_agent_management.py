import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import app.api.http as http
import app.api.people as people
import app.workspace.service as service_module
from app.auth import decrypt_secret
from app.band.platform import BandIdentity
from app.persistence.models import (AgentProfile, AgentTask, BandAgentOwner, User, UserBandAccount, UserBandConnection, db)
from app.tasks.queue import next_task
from app.tasks.worker import deliver, process

@pytest.fixture
def management_app(svc, monkeypatch, offline_band_platform):
    async def noop(*args): pass
    async def identity(key):
        return BandIdentity(key.removeprefix('band_a_'), 'Band agent', 'owner/agent')
    monkeypatch.setattr(svc, 'startup', noop)
    monkeypatch.setattr(svc, 'start_agent', noop)
    monkeypatch.setattr(http, 'service', svc)
    monkeypatch.setattr(people, 'service', svc)
    monkeypatch.setattr(service_module, 'service', svc)
    monkeypatch.setattr(service_module, 'fetch_identity', identity)
    monkeypatch.setattr(service_module, 'configuration', lambda: ('offline-key', ['gpt-6-luna'], 'gpt-6-luna'))
    with TestClient(http.app) as client:
        users = []
        for name in ('alice', 'bob'):
            result = client.post('/api/auth/signup', json={'username': name, 'display_name': name.title(),
                                                         'password': 'test-password'})
            assert result.status_code == 200, result.text
            users.append((result.json()['user']['id'], {'Authorization': 'Bearer ' + result.json()['token']}))
        yield client, users, offline_band_platform


def connect(client, headers, name):
    result = client.put('/api/band/account', headers=headers, json={'api_key': 'band_u_' + name})
    assert result.status_code == 200, result.text
    assert result.json()['can_manage_agents'] is True
    assert 'band_u_' not in result.text


def test_local_signup_and_shared_human_chat_need_no_band_key(management_app):
    client, ((alice, a), (bob, b)), network = management_app
    assert network.calls == []
    assert client.get('/api/bootstrap', headers=a).json()['band']['configured'] is False
    channel = client.post('/api/channels', headers=a, json={'name': 'Local team'}).json()
    assert channel['band_room_id'] is None
    request = client.post('/api/people/requests', headers=a, json={'handle': 'bob'}).json()
    assert client.post(f"/api/people/requests/{request['id']}/decision", headers=b, json={'accept': True}).status_code == 200
    invitation = client.post(f"/api/channels/{channel['id']}/invitations", headers=a, json={'user_id': bob})
    assert invitation.status_code == 200
    invite = client.get('/api/people/invitations', headers=b).json()[0]
    assert client.post(f"/api/people/invitations/{invite['id']}/decision", headers=b, json={'accept': True}).status_code == 200
    with client.websocket_connect('/ws?token=' + b['Authorization'][7:]) as socket:
        sent = client.post(f"/api/channels/{channel['id']}/messages", headers=a, json={'content': 'Hello Bob'})
        assert sent.status_code == 200
        event = socket.receive_json()
        while event['type'] != 'message':
            event = socket.receive_json()
        assert event['message']['content'] == 'Hello Bob'
    assert network.calls == []
    assert 'Hello Bob' in client.get(f"/api/channels/{channel['id']}/messages", headers=b).text


def test_each_user_registers_native_agents_with_only_their_own_key(management_app):
    client, users, network = management_app
    profiles = []
    for (user, headers), name in zip(users, ('alice', 'bob')):
        connect(client, headers, name)
        boot = client.get('/api/bootstrap', headers=headers)
        assert boot.json()['band']['can_manage_agents'] is True
        assert 'band_u_' not in boot.text
        created = client.post('/api/agents', headers=headers, json={'name': 'Writer', 'plugins': []})
        assert created.status_code == 200, created.text
        profile = created.json()
        assert profile['band_auth'] == 'managed' and profile['owner_id'] == user
        profiles.append(profile)
        with db() as session:
            account = session.get(UserBandAccount, user)
            assert account.secret_enc != 'band_u_' + name
            assert decrypt_secret(account.secret_enc) == 'band_u_' + name
            assert session.get(UserBandConnection, user) is None
            assert session.get(BandAgentOwner, profile['id']).band_user_id == name
            stored = session.get(AgentProfile, profile['id'])
            assert decrypt_secret(stored.band_api_key_enc) == 'band_a_' + profile['band_agent_id']
    assert profiles[0]['band_agent_id'] != profiles[1]['band_agent_id']
    registrations = [call for call in network.calls if call[0] == 'register']
    assert [call[1] for call in registrations] == ['band_u_alice', 'band_u_bob']
    assert client.delete(f"/api/agents/{profiles[0]['id']}", headers=users[1][1]).status_code in (403, 404)
    assert client.delete(f"/api/agents/{profiles[0]['id']}", headers=users[0][1]).status_code == 200
    assert ('delete', 'band_u_alice', profiles[0]['band_agent_id']) in network.calls
    assert profiles[1]['band_agent_id'] in network.managed_agents


def test_native_agent_works_in_local_channel_without_exporting_human_chat(management_app):
    client, ((owner, headers), _), network = management_app
    connect(client, headers, 'alice')
    channel = client.post('/api/channels', headers=headers, json={'name': 'Local agent work'}).json()
    created = client.post('/api/agents', headers=headers, json={'name': 'Writer', 'channel_id': channel['id']})
    assert created.status_code == 200, created.text
    assert client.post(f"/api/channels/{channel['id']}/messages", headers=headers,
                       json={'content': '@Writer prepare a draft'}).status_code == 200
    task = next_task()
    assert task.agent_id == created.json()['id'] and task.room_id == ''
    async def execute(task, saver): return 'A local draft.'
    asyncio.run(process(task, None, executor=execute))
    with db() as session:
        finished = session.get(AgentTask, task.id)
    asyncio.run(deliver(finished))
    assert 'A local draft.' in client.get(f"/api/channels/{channel['id']}/messages", headers=headers).text
    assert all(call[0] in {'management_identity', 'register'} for call in network.calls)


def test_signup_can_connect_user_key_atomically_or_skip_it(management_app):
    client, _, network = management_app
    result = client.post('/api/auth/signup', json={'username': 'cara', 'display_name': 'Cara',
        'password': 'test-password', 'band_api_key': 'band_u_cara'})
    assert result.status_code == 200, result.text
    with db() as session:
        assert session.get(UserBandAccount, result.json()['user']['id']).band_user_id == 'cara'
    network.rejected.add('band_u_rejected')
    result = client.post('/api/auth/signup', json={'username': 'failed', 'display_name': 'Failed',
        'password': 'test-password', 'band_api_key': 'band_u_rejected'})
    assert result.status_code == 422 and 'band_u_rejected' not in result.text
    with db() as session:
        assert session.exec(select(User).where(User.username == 'failed')).first() is None


def test_management_key_cannot_be_shared_or_replaced_with_another_identity(management_app):
    client, ((_, a), (_, b)), _ = management_app
    connect(client, a, 'alice')
    duplicate = client.put('/api/band/account', headers=b, json={'api_key': 'band_u_alice'})
    assert duplicate.status_code == 409
    replacement = client.put('/api/band/account', headers=a, json={'api_key': 'band_u_other'})
    assert replacement.status_code == 409
    assert client.put('/api/band/account', headers=a, json={'api_key': 'band_a_wrong-key'}).status_code == 422


def test_missing_or_revoked_management_never_falls_back_to_another_user(management_app):
    client, ((_, a), (_, b)), network = management_app
    connect(client, a, 'alice')
    assert client.post('/api/agents', headers=b, json={'name': 'Without connection'}).status_code == 409
    network.rejected.add('band_u_alice')
    result = client.post('/api/agents', headers=a, json={'name': 'Revoked connection'})
    assert result.status_code == 502 and 'band_u_alice' not in result.text
    with db() as session:
        profiles = session.exec(select(AgentProfile)).all()
        assert len(profiles) == 2 and all(profile.is_master for profile in profiles)
        assert all(not profile.band_api_key_enc for profile in profiles)


def test_name_conflict_does_not_register_an_extra_native_agent(management_app):
    client, ((_, headers), _), network = management_app
    connect(client, headers, 'alice')
    assert client.post('/api/agents', headers=headers, json={'name': 'Writer'}).status_code == 200
    assert client.post('/api/agents', headers=headers, json={'name': 'Writer'}).status_code == 409
    assert len([call for call in network.calls if call[0] == 'register']) == 1


@pytest.mark.parametrize('lookup_failure', ['timeout', 'identity_mismatch'])
def test_created_agent_key_survives_failed_identity_verification(management_app, monkeypatch, lookup_failure):
    client, ((owner, headers), _), network = management_app
    connect(client, headers, 'alice')

    async def identity(key):
        with db() as session:
            saved = session.exec(select(AgentProfile)).one()
            assert decrypt_secret(saved.band_api_key_enc) == key
            assert session.get(BandAgentOwner, saved.id).user_id == owner
        if lookup_failure == 'timeout':
            raise TimeoutError('Temporary identity lookup failure')
        return BandIdentity('unexpected-agent', 'Wrong identity', 'wrong/agent')

    monkeypatch.setattr(service_module, 'fetch_identity', identity)
    started = []
    async def start(profile): started.append(profile.id)
    monkeypatch.setattr(service_module.service, 'start_agent', start)
    result = client.post('/api/agents', headers=headers, json={'name': 'Saved Writer'})
    assert result.status_code == 200, result.text
    profile = result.json()
    assert profile['status']['state'] == 'error'
    assert 'key saved securely' in profile['status']['error']
    assert started == []
    assert 'band_a_' not in result.text
    assert client.post('/api/agents', headers=headers, json={'name': 'Saved Writer'}).status_code == 409
    assert len([call for call in network.calls if call[0] == 'register']) == 1


def test_two_users_and_their_own_agents_share_an_invited_local_channel(management_app):
    client, ((alice, a), (bob, b)), network = management_app
    connect(client, a, 'alice')
    connect(client, b, 'bob')
    writer = client.post('/api/agents', headers=a, json={'name': 'Writer'}).json()
    reviewer = client.post('/api/agents', headers=b, json={'name': 'Reviewer'}).json()
    channel = client.post('/api/channels', headers=a, json={'name': 'Joint work'}).json()
    request = client.post('/api/people/requests', headers=a, json={'handle': 'bob'}).json()
    client.post(f"/api/people/requests/{request['id']}/decision", headers=b, json={'accept': True})
    client.post(f"/api/channels/{channel['id']}/invitations", headers=a, json={'user_id': bob})
    invite = client.get('/api/people/invitations', headers=b).json()[0]
    client.post(f"/api/people/invitations/{invite['id']}/decision", headers=b, json={'accept': True})
    for headers, profile in ((a, writer), (b, reviewer)):
        added = client.post(f"/api/channels/{channel['id']}/agents", headers=headers, json={'agent_id': profile['id']})
        assert added.status_code == 200, added.text
    sent = client.post(f"/api/channels/{channel['id']}/messages", headers=b,
        json={'content': '@Writer @Reviewer work together', 'client_id': 'shared-request'})
    assert sent.status_code == 200, sent.text
    with db() as session:
        tasks = session.exec(select(AgentTask).where(AgentTask.channel_id == channel['id'])).all()
        assert {task.agent_id for task in tasks} == {writer['id'], reviewer['id']}
        assert all(task.room_id == '' for task in tasks)
    assert '@Writer @Reviewer work together' in client.get(f"/api/channels/{channel['id']}/messages", headers=a).text
    assert client.patch(f"/api/agents/{reviewer['id']}", headers=a, json={'instructions': 'Overwrite'}).status_code in (403, 404)
    assert all(call[0] in {'management_identity', 'register'} for call in network.calls)


def test_management_validation_errors_mask_rejected_credentials(management_app):
    client, ((_, headers), _), _ = management_app
    key = 'band_u_' + 's' * 2100
    result = client.put('/api/band/account', headers=headers, json={'api_key': key})
    assert result.status_code == 422
    assert 's' * 100 not in result.text


def test_agent_transport_failure_does_not_disable_separate_agent_management(management_app, monkeypatch):
    client, ((owner, headers), _), network = management_app
    connect(client, headers, 'alice')
    result = client.put('/api/band/connection', headers=headers, json={'api_key': 'band_a_transport-alice'})
    assert result.status_code == 200, result.text
    events = []
    async def publish(event, **kwargs): events.append(event)
    monkeypatch.setattr(service_module.bus, 'publish', publish)
    asyncio.run(service_module.service.platform_connection_failed(owner, 'transport-alice'))
    assert events[-1]['band']['can_manage_agents'] is True
    assert events[-1]['band']['online'] is False
    network.rejected.add('band_a_transport-alice')
    created = client.post('/api/agents', headers=headers, json={'name': 'Independent Writer'})
    assert created.status_code == 200, created.text
    assert created.json()['band_auth'] == 'managed'
