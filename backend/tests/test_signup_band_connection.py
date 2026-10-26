from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

import app.band.connections as connections
from app.auth import decrypt_secret
from app.band.platform import BandIdentity
from app.main import app
from app.persistence.models import AgentProfile, User, UserBandAccount, UserBandConnection, db, init_db


def payload(key=None):
    name = 'signup-' + uuid4().hex[:12]
    body = {'username': name, 'display_name': 'Signup user', 'password': 'private-password'}
    if key is not None:
        body['band_api_key'] = key
    return body


def user_exists(name):
    with db() as session:
        return session.exec(select(User.id).where(User.username == name)).first() is not None


@pytest.mark.parametrize('key', ['sk-model-key', 'band_a_' + 's' * 2000])
def test_signup_redacts_invalid_optional_credentials(key):
    init_db()
    body = payload(key)
    with TestClient(app) as client:
        result = client.post('/api/auth/signup', json=body)
    assert result.status_code == 422
    assert result.json()['field'] == 'band_api_key'
    assert body['password'] not in result.text
    if key:
        assert key not in result.text
    assert not user_exists(body['username'])


@pytest.mark.parametrize('kind', ['omitted', 'empty', 'user'])
def test_signup_accepts_local_accounts_and_optional_management_key(kind):
    init_db()
    key = None if kind == 'omitted' else '' if kind == 'empty' else 'band_u_' + uuid4().hex
    body = payload(key)
    with TestClient(app) as client:
        result = client.post('/api/auth/signup', json=body)
        assert result.status_code == 200, result.text
        user_id = result.json()['user']['id']
        with db() as session:
            assert session.get(UserBandConnection, user_id) is None
            account = session.get(UserBandAccount, user_id)
            if kind == 'user':
                assert decrypt_secret(account.secret_enc) == key
                assert key not in result.text
            else:
                assert account is None


def test_signup_stores_a_platform_connection_and_its_private_app_master():
    init_db()
    key = 'band_a_' + uuid4().hex
    body = payload('  ' + key + '  ')
    with db() as session:
        before = len(session.exec(select(AgentProfile.id)).all())
    with TestClient(app) as client:
        result = client.post('/api/auth/signup', json=body)
        assert result.status_code == 200, result.text
        assert key not in result.text
        headers = {'Authorization': f"Bearer {result.json()['token']}"}
        status = client.get('/api/band/connection', headers=headers)
        assert status.status_code == 200 and status.json()['connected'] is True
        assert status.json()['api'] == 'agent' and key not in status.text
        assert client.get('/api/band/connection').status_code == 401
        assert client.post('/api/auth/login', json={'username': body['username'], 'password': body['password']}).status_code == 200
        with db() as session:
            user_id = result.json()['user']['id']
            connection = session.get(UserBandConnection, user_id)
            assert decrypt_secret(connection.secret_enc) == key and connection.secret_enc != key
            assert session.get(UserBandAccount, user_id) is None
            assert len(session.exec(select(AgentProfile.id)).all()) == before + 1
            master = session.exec(select(AgentProfile).where(AgentProfile.created_by == user_id,
                                                           AgentProfile.is_master == True)).one()
            assert master.band_api_key_enc == "" and master.band_agent_id == f"local-master-{user_id}"


def test_signup_validation_failure_leaves_no_account_and_can_retry(monkeypatch):
    init_db()
    key = 'band_a_' + uuid4().hex
    body = payload(key)
    original = connections.fetch_identity
    async def reject(_key):
        raise ValueError('Provider response containing ' + key)
    monkeypatch.setattr(connections, 'fetch_identity', reject)
    with TestClient(app) as client:
        result = client.post('/api/auth/signup', json=body)
        assert result.status_code == 422 and key not in result.text
        assert not user_exists(body['username'])
        monkeypatch.setattr(connections, 'fetch_identity', original)
        assert client.post('/api/auth/signup', json=body).status_code == 200


def test_band_connection_cannot_be_claimed_by_another_user_and_signup_rolls_back():
    init_db()
    key = 'band_a_' + uuid4().hex
    first, second = payload(key), payload(key)
    with TestClient(app) as client:
        original = client.post('/api/auth/signup', json=first)
        assert original.status_code == 200
        duplicate = client.post('/api/auth/signup', json=second)
        assert duplicate.status_code == 409 and duplicate.json()['field'] == 'band_api_key'
        assert key not in duplicate.text and not user_exists(second['username'])
        second['band_api_key'] = 'band_a_' + uuid4().hex
        other = client.post('/api/auth/signup', json=second)
        assert other.status_code == 200
        with db() as session:
            a = session.get(UserBandConnection, original.json()['user']['id'])
            b = session.get(UserBandConnection, other.json()['user']['id'])
            assert decrypt_secret(a.secret_enc) == key
            assert decrypt_secret(b.secret_enc) == second['band_api_key']


def test_connection_updates_keep_the_same_band_identity(monkeypatch):
    init_db()
    key = 'band_a_' + uuid4().hex
    with TestClient(app) as client:
        result = client.post('/api/auth/signup', json=payload(key)).json()
        headers = {'Authorization': f"Bearer {result['token']}"}
        changed = client.put('/api/band/connection', headers=headers, json={'api_key': 'band_a_other-identity'})
        assert changed.status_code == 409
        async def rotated(_key):
            return BandIdentity(id=key.removeprefix('band_a_'), name='Updated connection', handle='same-identity')
        monkeypatch.setattr(connections, 'fetch_identity', rotated)
        next_key = 'band_a_rotated-' + uuid4().hex
        saved = client.put('/api/band/connection', headers=headers, json={'api_key': next_key})
        assert saved.status_code == 200 and next_key not in saved.text
        with db() as session:
            assert decrypt_secret(session.get(UserBandConnection, result['user']['id']).secret_enc) == next_key


def test_platform_client_uses_the_requesting_accounts_credential(monkeypatch):
    import asyncio
    init_db()
    key = 'band_a_' + uuid4().hex
    with TestClient(app) as client:
        result = client.post('/api/auth/signup', json=payload(key)).json()
    seen = []
    class Client:
        def __init__(self, *, api_key, base_url): seen.append(api_key)
    async def close(client): seen.append('closed')
    monkeypatch.setattr(connections, 'AsyncRestClient', Client)
    monkeypatch.setattr(connections, 'aclose_rest_client', close)
    async def use():
        async with connections.platform_client(result['user']['id']):
            pass
    asyncio.run(use())
    assert seen == [key, 'closed']
