"""Offline contracts for durable disks, provider routing, privacy and safe detach."""
import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from tenki_sandbox.errors import SessionNotFoundError
from app.auth import encrypt_secret, issue_token
from app.computer import computer_key
from app.computer.e2b import DesktopOptions
from app.computer.ingress import endpoint, headers
from app.computer.providers import ComputerMissing, ComputerProviderError, provider_for
from app.computer.tenki import TenkiDesktopClient, TenkiDesktopSession, WORKSPACE
from app.config import settings
from app.persistence.models import (AgentTask, ComputerDisk, ComputerEndpoint, ComputerScope,
                                    TaskComputerBinding, db, init_db, utcnow)
from app.main import app
from app.tasks import enqueue
from helpers import fake_tenki_remote, fake_tenki_sdk, tenki_config

def test_volume_allocates_once_and_is_reused_after_vm_loss():
    init_db()
    sdk=fake_tenki_sdk()
    client=TenkiDesktopClient(config=tenki_config(),sdk=sdk)
    key=uuid4().hex
    volume=SimpleNamespace(id="v-"+key,name="band-data-"+hashlib.sha256(key.encode()).hexdigest()[:32],active_attachments=[])
    sdk.volumes.create.return_value=volume
    sdk.volumes.wait_ready.return_value=volume
    sdk.volumes.get.return_value=volume
    async def scenario():
        assert (await client.volume(key)).id==volume.id
        assert (await client.volume(key)).id==volume.id
        sdk.volumes.create.assert_awaited_once()
        with db() as session:
            assert session.get(ComputerDisk,key).volume_id==volume.id
    asyncio.run(scenario())


def test_uncertain_disk_creation_cannot_allocate_a_duplicate():
    init_db()
    key=uuid4().hex
    name="band-data-"+hashlib.sha256(key.encode()).hexdigest()[:32]
    with db() as session:
        session.add(ComputerDisk(key=key,name=name,state="creating"))
        session.commit()
    sdk=fake_tenki_sdk()
    client=TenkiDesktopClient(config=tenki_config(),sdk=sdk)
    with pytest.raises(ComputerProviderError,match="uncertain"):
        asyncio.run(client.volume(key))
    sdk.volumes.create.assert_not_awaited()


def test_volume_name_mismatch_is_rejected():
    init_db()
    key=uuid4().hex
    name="band-data-"+hashlib.sha256(key.encode()).hexdigest()[:32]
    with db() as session:
        session.add(ComputerDisk(key=key,name=name,volume_id="other",state="ready"))
        session.commit()
    sdk=fake_tenki_sdk()
    sdk.volumes.get.return_value=SimpleNamespace(name="someone-else")
    with pytest.raises(ComputerProviderError,match="ownership"):
        asyncio.run(TenkiDesktopClient(config=tenki_config(),sdk=sdk).volume(key))


def test_vm_create_mounts_saved_disk_enables_internet_and_sets_deadline():
    sdk=fake_tenki_sdk()
    remote=fake_tenki_remote()
    sdk.create.return_value=remote
    client=TenkiDesktopClient(config=tenki_config(),sdk=sdk)
    client.volume=AsyncMock(return_value=SimpleNamespace(id="disk",active_attachments=[]))
    async def scenario():
        await client.create(DesktopOptions("name","unused",{"workspace":"work","agent":"a","channel":"7"},1200))
        options=sdk.create.call_args.kwargs
        assert options['volumes']==[{"volume_id":"disk","mount_path":WORKSPACE}]
        assert options['allow_outbound'] and options['allow_inbound']
        assert options['max_duration']==1200 and options['pause_retention']==604800
        assert options['cpu_cores']==2 and options['memory_mb']==4096
        assert options['metadata']['data_volume']=='disk'
        assert options['snapshot_id']=='clean' and 'env' not in options
    asyncio.run(scenario())


def test_attached_disk_blocks_duplicate_vm_allocation():
    sdk=fake_tenki_sdk()
    client=TenkiDesktopClient(config=tenki_config(),sdk=sdk)
    client.volume=AsyncMock(return_value=SimpleNamespace(active_attachments=[object()]))
    with pytest.raises(ComputerProviderError,match="attached"):
        asyncio.run(client.create(DesktopOptions('name','image',{'workspace':'w','agent':'a','channel':'c'},1200)))
    sdk.create.assert_not_awaited()


def test_lookup_never_resumes_before_controller_ownership_checks():
    sdk=fake_tenki_sdk()
    remote=fake_tenki_remote()
    sdk.get.return_value=remote
    session=asyncio.run(TenkiDesktopClient(config=tenki_config(),sdk=sdk).get('vm'))
    assert session.labels['agent']=='agent'
    remote.resume.assert_not_awaited()


def test_pause_waits_for_durable_state_and_detach_never_terminates():
    sdk=fake_tenki_sdk()
    client=TenkiDesktopClient(config=tenki_config(),sdk=sdk)
    remote=fake_tenki_remote()
    remote.state='RUNNING'
    async def paused(_timeout):remote.state='PAUSED'
    remote.wait_paused.side_effect=paused
    session=TenkiDesktopSession(client,remote)
    client.sessions.append(session)
    session.prepare_home=AsyncMock()
    async def scenario():
        await session.stop(timeout=180)
        assert session.state=='stopped'
        remote.wait_paused.assert_awaited_once_with(180)
        session.prepare_home.assert_awaited_once()
        await client.close()
        remote.close.assert_not_awaited()
        remote.detach.assert_called_once()
        remote._reset_data_plane.assert_awaited_once()
        sdk.close.assert_awaited_once()
    asyncio.run(scenario())


def test_resume_refuses_missing_disk_attachment():
    init_db()
    client=TenkiDesktopClient(config=tenki_config(),sdk=fake_tenki_sdk())
    remote=fake_tenki_remote()
    remote.info.volume_mounts=[]
    session=TenkiDesktopSession(client,remote)
    with pytest.raises(ComputerProviderError,match='not mounted'):
        asyncio.run(session.start())


def test_resume_verifies_saved_disk_before_reading_guest_files():
    init_db()
    sdk=fake_tenki_sdk()
    client=TenkiDesktopClient(config=tenki_config(),sdk=sdk)
    remote=fake_tenki_remote()
    remote.info.metadata['agent']=uuid4().hex
    session=TenkiDesktopSession(client,remote)
    key='workspace:'+session.key
    with db() as sql:
        sql.add(ComputerDisk(key=key,name='owned',volume_id='wrong',state='ready'))
        sql.commit()
    session.prepare_home=AsyncMock()
    with pytest.raises(ComputerProviderError,match='verified ownership'):
        asyncio.run(session.start())
    session.prepare_home.assert_not_awaited()


def test_missing_vm_is_reported_without_resuming_or_allocating():
    sdk=fake_tenki_sdk()
    sdk.get.side_effect=SessionNotFoundError('missing')
    with pytest.raises(ComputerMissing):
        asyncio.run(TenkiDesktopClient(config=tenki_config(),sdk=sdk).get('expired'))
    sdk.create.assert_not_awaited()


def test_unconfirmed_pause_is_not_reported_as_stopped():
    sdk=fake_tenki_sdk()
    client=TenkiDesktopClient(config=tenki_config(),sdk=sdk)
    remote=fake_tenki_remote()
    remote.state='RUNNING'
    session=TenkiDesktopSession(client,remote)
    session.prepare_home=AsyncMock()
    with pytest.raises(ComputerProviderError,match='confirmed'):
        asyncio.run(session.stop())
    assert session.state=='started'


def test_routing_is_pinned_and_old_tasks_keep_e2b(desktop_job):
    task,agent,_,_,_,_=desktop_job
    assert provider_for(task)=='e2b'
    with db() as session:
        scope=session.get(ComputerScope,f'agent-{agent.band_agent_id}-channel-{task.channel_id}')
        scope.provider='tenki'
        session.add(scope)
        session.commit()
    fresh=enqueue(agent_id=agent.id,channel_id=task.channel_id,room_id=task.room_id,source_id=uuid4().hex,prompt='New task')
    assert provider_for(fresh)=='tenki' and computer_key(fresh).startswith('tenki-')
    assert provider_for(task)=='e2b' and computer_key(task).startswith('e2b-')


def test_init_backfills_old_tasks_as_e2b(desktop_job,monkeypatch):
    import app.persistence.models as models
    task=desktop_job[0]
    with db() as session:
        session.delete(session.get(TaskComputerBinding,task.id))
        session.commit()
    monkeypatch.setattr(models,'settings',replace(settings,computer_provider='tenki'))
    init_db()
    assert provider_for(task)=='e2b'


def test_legacy_binding_backfill_tolerates_another_process_winning_the_insert(desktop_job, monkeypatch):
    from app.persistence.models import bind_computer
    task = desktop_job[0]
    with db() as session:
        original = session.get(TaskComputerBinding, task.id)
        expected = (original.provider, original.computer_key)
    with db() as session:
        # The API already inserted the row after the worker read its unbound-task list.
        get = session.get
        monkeypatch.setattr(session, "get", lambda model, key, *a, **kw:
            None if model is TaskComputerBinding else get(model, key, *a, **kw))
        bind_computer(session, task, legacy=True)
        session.commit()
    with db() as session:
        binding = session.get(TaskComputerBinding, task.id)
        assert (binding.provider, binding.computer_key) == expected


def test_rolled_back_task_does_not_leave_a_computer_binding(desktop_job):
    original = desktop_job[0]
    task = AgentTask(id=uuid4().hex, source_key=uuid4().hex, agent_id=original.agent_id,
                     channel_id=original.channel_id, room_id="offline", prompt="Rolled-back work")
    with db() as session:
        session.add(task)
        session.flush()
        assert session.get(TaskComputerBinding, task.id) is not None
        session.rollback()
    with db() as session:
        assert session.get(AgentTask, task.id) is None
        assert session.get(TaskComputerBinding, task.id) is None


def test_profile_reports_selected_scope_instead_of_historical_vm(desktop_job,monkeypatch):
    import app.api.http as http
    task,agent,user,_,_,_=desktop_job
    with db() as session:
        scope=session.get(ComputerScope,f'agent-{agent.band_agent_id}-channel-{task.channel_id}')
        scope.provider='tenki'
        session.add(scope)
        session.commit()
    monkeypatch.setattr(http,'settings',tenki_config())
    with TestClient(app) as client:
        data=client.get(f'/api/agents/{agent.id}/computers',headers={'Authorization':'Bearer '+issue_token(user.id)}).json()
    scope=data['computers'][0]
    assert scope['provider']=='tenki' and scope['configured'] and scope['task_id'] is None


def test_private_endpoint_checks_scope_vm_and_expiry():
    init_db()
    key='tenki-agent-'+uuid4().hex
    with db() as session:
        session.add(ComputerEndpoint(key=key,port=3000,sandbox_id='owned',expires_at=utcnow()+timedelta(minutes=5),
            endpoint_enc=encrypt_secret(json.dumps({'origin':'https://preview.tenki.sh','secret':'a'*64}))))
        session.commit()
    assert endpoint(key,'owned',3000)==('preview.tenki.sh','a'*64)
    for other_key,vm,port in [(key,'wrong',3000),(key,'owned',3001),(key+'-other','owned',3000)]:
        with pytest.raises(HTTPException):
            endpoint(other_key,vm,port)
    assert headers(key,'secret',6081)=={'x-band-ingress':'secret','x-band-port':'6081'}


def test_http_proxy_discards_forged_provider_headers():
    from app.computer.app_proxy import request_headers
    preview=SimpleNamespace(computer_key='tenki-agent-a-channel-1',port=3000,request_host='localhost:3000')
    result=request_headers({'x-band-ingress':'forged','x-band-port':'6080','e2b-traffic-access-token':'forged'},preview,'server-secret','https://app.test')
    assert result['x-band-ingress']=='server-secret' and result['x-band-port']=='3000'
    assert 'e2b-traffic-access-token' not in result


def test_tenki_key_is_masked_in_audit():
    from app.workspace.audit import summarize
    assert 'tk_abcdefgh12345678' not in summarize('credential tk_abcdefgh12345678')


def test_selection_preserves_waiting_task_but_refuses_queued_work(desktop_job,monkeypatch):
    import app.computer.select as selector
    task,agent,_,_,_,_=desktop_job
    monkeypatch.setattr(selector,'configured',lambda provider: True)
    with db() as session:
        stored=session.get(AgentTask,task.id)
        stored.status='waiting'
        session.add(stored)
        session.commit()
    assert selector.select_provider('tenki',agent_id=agent.id,channel_id=task.channel_id,preserve_waiting=True)==1
    assert provider_for(task)=='e2b'
    with db() as session:
        assert session.get(AgentTask,task.id).status=='waiting'
    fresh=enqueue(agent_id=agent.id,channel_id=task.channel_id,room_id=task.room_id,source_id=uuid4().hex,prompt='New work')
    assert provider_for(fresh)=='tenki'
    with pytest.raises(ValueError,match='Drain'):
        selector.select_provider('e2b',agent_id=agent.id,channel_id=task.channel_id,preserve_waiting=True)


def test_operator_preparation_does_not_enqueue_model_work_or_send_a_message(desktop_job,monkeypatch):
    import app.computer.prepare as operator
    from app.persistence.models import ChannelMember, Message
    from sqlmodel import select
    task,agent,_,_,_,_=desktop_job
    with db() as session:
        stored=session.get(AgentTask,task.id)
        stored.status='completed'
        session.add(stored)
        session.add(ChannelMember(channel_id=task.channel_id,member_type='agent',member_id=agent.id))
        session.commit()
        before=len(session.exec(select(Message.id)).all())
    fake=SimpleNamespace(screenshot=AsyncMock(),close=AsyncMock())
    monkeypatch.setattr(operator,'TaskComputer',lambda task:fake)
    prepared=asyncio.run(operator.prepare(agent.id,task.channel_id))
    with db() as session:
        stored=session.get(AgentTask,prepared)
        assert stored.status=='completed' and stored.delivery_status=='sent'
        assert len(session.exec(select(Message.id)).all())==before
    assert asyncio.run(operator.prepare(agent.id,task.channel_id))==prepared
    fake.close.assert_awaited_with(force=True)


def test_viewer_cleanup_accepts_a_client_that_disconnected_during_close(monkeypatch):
    import app.computer.viewer as viewer
    from contextlib import asynccontextmanager
    from fastapi import WebSocketDisconnect
    monkeypatch.setattr(viewer,'viewer_route',lambda token:('private.host',{},1,('private.host',6081)))
    async def receive():return {'type':'websocket.disconnect'}
    async def messages():
        if False:
            yield b''
    class Stream:
        def __aiter__(self):return messages()
    @asynccontextmanager
    async def connection(*args,**kwargs):yield Stream()
    monkeypatch.setattr(viewer,'connect',connection)
    socket=SimpleNamespace(accept=AsyncMock(),receive=receive,close=AsyncMock(side_effect=WebSocketDisconnect(1006)))
    asyncio.run(viewer.desktop_socket(socket,'signed'))


@pytest.mark.parametrize("confirmed", [True, False])
def test_recovered_pending_pause_requires_provider_confirmation(confirmed):
    remote = fake_tenki_remote()
    remote.state = "PAUSING"
    session = TenkiDesktopSession(TenkiDesktopClient(config=tenki_config(), sdk=fake_tenki_sdk()), remote)
    session.prepare_home = AsyncMock()
    assert session.state != "stopped"
    if confirmed:
        async def paused(_timeout):
            remote.state = "PAUSED"
        remote.wait_paused.side_effect = paused
        asyncio.run(session.stop())
        assert session.state == "stopped"
    else:
        with pytest.raises(ComputerProviderError, match="confirmed"):
            asyncio.run(session.stop())
        assert session.state != "stopped"
    remote.wait_paused.assert_awaited_once()
    remote.pause.assert_not_awaited()
    session.prepare_home.assert_not_awaited()
