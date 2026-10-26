"""Explicit worker-side checks. Ordinary reads never allocate a computer."""
import asyncio
import shlex
import httpx
from sqlmodel import select
from sqlalchemy import update
from app.persistence.models import db, ComputerDisk
from app.providers.models import ProviderOperation, ProviderRevision
from app.providers.connections import resolve
from app.providers.contracts import ProviderError
from app.providers.language_models import verify_models

def journal(identity: str, **values):
    with db() as session:
        row = session.get(ProviderOperation, identity)
        row.result = {**row.result, **values}
        session.add(row)
        session.commit()

async def computer_access(connection):
    if connection.provider == "tenki":
        from tenki import AsyncClient
        async with AsyncClient(api_key=connection.secret["api_key"], warning_handler=None) as sdk:
            await sdk.list()
            image = connection.config["image"]
            if image.startswith("snapshot:"):
                await sdk.snapshots.get(image[9:])
    else:
        from e2b import AsyncSandbox
        pages = AsyncSandbox.list(api_key=connection.secret["api_key"], request_timeout=20)
        await pages.next_items()

async def cleanup_probe(operation):
    """Only resources named and journaled by this exact operation can be removed."""
    from app.providers.computers import config_from, client
    values = operation.result
    expected = "band-provider-check-" + operation.id
    if values.get("probe_name") != expected or values.get("probe_owner") != operation.id:
        raise ValueError("This operation does not own a computer probe")
    config = config_from(operation.binding)
    driver = client(operation.binding["provider"], config)
    try:
        if operation.binding["provider"] == "tenki":
            found = [vm for vm in await driver.sdk.list() if vm.name == expected]
            for vm in found:
                if vm.info.metadata.get("probe_owner") != operation.id:
                    raise ValueError("Probe ownership mismatch")
                await vm.close_if_open()
            key = values.get("probe_disk_key")
            with db() as session:
                disk = session.get(ComputerDisk, key) if key else None
            if disk and disk.volume_id:
                volume = await driver.sdk.volumes.get(disk.volume_id)
                if volume.name != disk.name:
                    raise ValueError("Probe disk ownership mismatch")
                for _ in range(60):
                    if not volume.active_attachments:
                        break
                    await asyncio.sleep(1)
                    volume = await driver.sdk.volumes.get(disk.volume_id)
                if volume.active_attachments:
                    raise ValueError("Probe disk is still attached")
                await driver.sdk.volumes.delete(volume.id)
                with db() as session:
                    current = session.get(ComputerDisk, key)
                    if current:
                        session.delete(current)
                    session.commit()
        else:
            from e2b import AsyncSandbox, NotFoundException
            identity = values.get("probe_sandbox_id")
            if identity:
                try:
                    info = await AsyncSandbox.get_info(identity, api_key=config.e2b_api_key)
                    if info.metadata.get("probe_owner") != operation.id or info.metadata.get("computer_name") != expected:
                        raise ValueError("Probe ownership mismatch")
                    await AsyncSandbox.kill(identity, api_key=config.e2b_api_key)
                except NotFoundException:
                    pass
            elif values.get("allocation_requested"):
                # Recover an allocation whose create response was lost by its unique owned name.
                from e2b import SandboxQuery, SandboxState
                pages = AsyncSandbox.list(SandboxQuery(metadata={"probe_owner": operation.id},
                    state=[SandboxState.RUNNING, SandboxState.PAUSED]), api_key=config.e2b_api_key)
                for info in await pages.next_items():
                    if info.metadata.get("computer_name") != expected:
                        raise ValueError("Probe ownership mismatch")
                    await AsyncSandbox.kill(info.sandbox_id, api_key=config.e2b_api_key)
        journal(operation.id, cleanup="confirmed")
    finally:
        await driver.close()

async def computer_probe(operation, connection):
    from app.providers.computers import config_from, client
    from app.computer.e2b import DesktopOptions
    from app.computer.identity import key_from_labels
    from types import SimpleNamespace
    values = vars(config_from(operation.binding)).copy()
    # One GiB disposable disk, no application workspace identities or credentials in the guest.
    values.update(tenki_disk_gib=1, tenki_timeout=600, e2b_timeout=600)
    config = SimpleNamespace(**values)
    driver = client(connection.provider, config)
    labels = {"app": "band-provider-check", "workspace": operation.id, "agent": "probe", "channel": "0", "probe_owner": operation.id}
    disk_key = operation.id + ":" + key_from_labels(labels)
    name = "band-provider-check-" + operation.id
    journal(operation.id, probe_name=name, probe_owner=operation.id, probe_disk_key=disk_key, allocation_requested=True)
    try:
        sandbox = await driver.create(DesktopOptions(name, config.tenki_desktop_image if connection.provider == "tenki" else config.e2b_template, labels, 600))
        journal(operation.id, probe_sandbox_id=sandbox.id)
        await sandbox.start()
        await sandbox.computer_use.start()
        info = await sandbox.computer_use.display.get_info()
        if not info.displays:
            raise ValueError("No desktop display")
        work = await sandbox.get_work_dir()
        marker = work.rstrip("/") + "/.band-provider-marker"
        await sandbox.fs.upload_file(operation.id, marker)
        script = "import urllib.request; urls=['https://example.com','https://github.com','https://registry.npmjs.org','https://pypi.org']; " + "[urllib.request.urlopen(u,timeout=15).close() for u in urls]; print('NETWORK_OK')"
        result = await sandbox.process.exec("python3 -c " + shlex.quote(script), timeout=75)
        if result.exit_code or "NETWORK_OK" not in result.result:
            raise ValueError("Internet access failed")
        await sandbox.computer_use.keyboard.press("Super_L")
        shot = await sandbox.computer_use.screenshot.take_compressed()
        if not shot.screenshot:
            raise ValueError("Desktop observation failed")
        if connection.provider == "tenki":
            await sandbox.open_browser("https://example.com")
        else:
            await sandbox.checked("DISPLAY=:0 xdg-open https://example.com >/tmp/band-probe-browser.log 2>&1 &")
        await sandbox.stop(timeout=180 if connection.provider == "tenki" else 20)
        sandbox_id = sandbox.id
        await driver.close()
        driver = client(connection.provider, config)
        sandbox = await driver.get(sandbox_id)
        await sandbox.start()
        if (await sandbox.fs.download_file(marker)).decode() != operation.id:
            raise ValueError("Pause/resume lost the probe file")
        return {"desktop": True, "network": True, "files": True, "pause_resume": True}
    finally:
        await driver.close()
        with db() as session:
            current = session.get(ProviderOperation, operation.id)
        try:
            await cleanup_probe(current)
        except Exception:
            journal(operation.id, cleanup="needs_attention")
            raise ProviderError("computer", "probe_cleanup", detail="Computer verification needs cleanup before activation") from None

async def verify(operation):
    connection = resolve(operation.binding, "verification")
    provider = connection.provider
    verified = {"passed": True, "models": {}}
    if provider in {"openai", "anthropic", "gemini", "compatible"}:
        models = connection.config.get("models", [])
        if models:
            verified["models"] = await verify_models(connection, models)
        image = connection.config.get("image_model")
        if image:
            # Authentication/catalog access alone does not prove image output.
            # This is an explicitly initiated, small billable verification request.
            from app.providers.images import request_image
            data, _ = await request_image(connection, image, "Generate a small solid red square for an API connection test.")
            from PIL import Image
            import io
            with Image.open(io.BytesIO(data)) as result:
                result.verify()
            verified["image_model"] = image
        if provider == "openai":
            from openai import AsyncOpenAI
            async with AsyncOpenAI(api_key=connection.secret["api_key"], base_url="https://api.openai.com/v1", max_retries=0, timeout=30) as sdk:
                await sdk.vector_stores.list(limit=1)
            verified["retrieval"] = True
        elif not models and not image:
            raise ValueError("Configure models before verification")
    elif provider in {"tenki", "e2b"}:
        await computer_access(connection)
        verified["access"] = True
        if operation.result.get("live_computer"):
            verified.update(await computer_probe(operation, connection))
    elif provider in {"exa", "tavily"}:
        from app.providers.research import search
        await search(connection, "provider connection check", limit=1)
    elif provider == "composio":
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get("https://backend.composio.dev/api/v3/connected_accounts", headers={"x-api-key": connection.secret["api_key"]}, params={"limit": 1})
            response.raise_for_status()
    return verified

async def process_one():
    with db() as session:
        operation = session.exec(select(ProviderOperation).where(ProviderOperation.service.in_(["verification", "index_cleanup"]),
            ProviderOperation.status == "pending").order_by(ProviderOperation.created_at)).first()
        if not operation:
            return False
        claimed = session.execute(update(ProviderOperation).where(ProviderOperation.id == operation.id,
            ProviderOperation.status == "pending").values(status="running"))
        if claimed.rowcount != 1:
            return True
        session.commit()
    try:
        async with asyncio.timeout(600):
            if operation.service == "index_cleanup":
                from app.providers.retrieval import release_connection_indexes
                await release_connection_indexes(operation.binding["connection_id"])
                verified = None
            elif operation.result.get("cleanup_only"):
                await cleanup_probe(operation)
                verified = None
            else:
                verified = await verify(operation)
        with db() as session:
            row = session.get(ProviderOperation, operation.id)
            if verified:
                revision = session.get(ProviderRevision, (operation.binding["connection_id"], operation.binding["revision"]))
                revision.verification = verified
                session.add(revision)
            row.status = "completed"
            row.result = {**row.result, "verification": verified}
            session.add(row)
            session.commit()
    except asyncio.CancelledError:
        with db() as session:
            row = session.get(ProviderOperation, operation.id)
            row.status = "uncertain"
            row.result = {**row.result, "error": "Verification was interrupted. Inspect or clean up its test resources before retrying."}
            session.add(row)
            session.commit()
        raise
    except Exception:
        with db() as session:
            row = session.get(ProviderOperation, operation.id)
            row.status = "uncertain" if row.result.get("cleanup") == "needs_attention" else "failed"
            row.result = {**row.result, "error": "Verification failed. Check credentials, model capabilities, billing, image access and network permissions."}
            session.add(row)
            session.commit()
    from app.workspace.events import bus
    await bus.publish({"type": "provider_settings_changed"})
    return True

def recover():
    with db() as session:
        for operation in session.exec(select(ProviderOperation).where(ProviderOperation.status == "running")).all():
            operation.status = "uncertain"
            operation.result = {**operation.result, "error": "Worker restarted during a provider request. Its outcome is uncertain; review it before starting a separate request."}
            session.add(operation)
        session.commit()
