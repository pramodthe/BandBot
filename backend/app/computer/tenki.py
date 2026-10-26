"""Tenki desktop with a durable data disk and authenticated, server-only ingress."""
from __future__ import annotations

import asyncio
import hashlib
import json
import posixpath
import secrets
import shlex
from datetime import timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from tenki import AsyncClient
from tenki_sandbox.errors import SessionNotFoundError

from app.auth import decrypt_secret, encrypt_secret
from app.config import settings
from app.computer.e2b import DesktopSession, jpeg_base64
from app.computer.providers import TRANSFER_LIMIT, ComputerMissing, ComputerProviderError
from app.computer.identity import key_from_labels
from app.computer.tenki_home import home_script
from app.persistence.models import ComputerDisk, ComputerEndpoint, ComputerGateway, db, insert_ignore, utcnow

WORKSPACE = "/home/tenki/workspace"


async def checked_sdk(operation):
    try:
        return await operation
    except SessionNotFoundError:
        raise ComputerMissing("Computer no longer exists") from None
    except Exception:
        raise ComputerProviderError("Computer provider request failed") from None


class TenkiDesktopClient:
    def __init__(self, *, config=settings, sdk=None):
        self.config = config
        self.sdk = sdk or AsyncClient(api_key=config.tenki_api_key, timeout=config.tenki_lifecycle_timeout,
                                      warning_handler=None)
        self.sessions = []

    async def get(self, reference: str, *, request_timeout=30):
        if reference.startswith("band-tenki-"):
            found = [s for s in await checked_sdk(self.sdk.list()) if s.name == reference
                     and s.state not in {"TERMINATED", "TERMINATING", "FAILED"}]
            if not found:
                raise ComputerMissing("Computer not found")
            if len(found) != 1:
                raise ComputerProviderError("Ambiguous computer identity")
            remote = found[0]
        else:
            remote = await checked_sdk(self.sdk.get(reference))
        if remote.state in {"TERMINATED", "TERMINATING", "FAILED"}:
            raise ComputerMissing("Computer no longer exists")
        session = TenkiDesktopSession(self, remote)
        self.sessions.append(session)
        return session  # Ownership is checked by TaskComputer before start/resume.

    async def volume(self, key: str):
        name = "band-data-" + hashlib.sha256(key.encode()).hexdigest()[:32]
        with db() as session:
            row = session.get(ComputerDisk, key)
            if row is None:
                row = ComputerDisk(key=key, name=name)
                session.add(row)
                session.commit()
            if row.name != name or row.state == "retired":
                raise ComputerProviderError("Durable disk ownership is unavailable")
            volume_id, state = row.volume_id, row.state
        if volume_id:
            volume = await checked_sdk(self.sdk.volumes.get(volume_id))
            if volume.name != name:
                raise ComputerProviderError("Durable disk ownership did not match")
            return volume
        matches = [v for v in await checked_sdk(self.sdk.volumes.list()) if v.name == name]
        if len(matches) > 1:
            raise ComputerProviderError("Ambiguous durable disk identity")
        if not matches and state != "new":
            raise ComputerProviderError("Disk creation is uncertain; inspect the provider before retrying")
        if matches:
            volume = matches[0]
        else:
            with db() as session:
                row = session.get(ComputerDisk, key)
                row.state = "creating"
                session.add(row)
                session.commit()
            # Persist intent first. A timeout must never allocate another disk blindly.
            volume = await checked_sdk(self.sdk.volumes.create(name=name,
                size_bytes=self.config.tenki_disk_gib * 1024 ** 3))
        with db() as session:
            row = session.get(ComputerDisk, key)
            row.volume_id, row.state = volume.id, "ready"
            session.add(row)
            session.commit()
        return await checked_sdk(self.sdk.volumes.wait_ready(volume.id, timeout=120))

    async def create(self, options, *, timeout=60):
        key = key_from_labels(options.labels)
        # Include workspace identity in the disk name, as well as agent/channel identity.
        disk_key = options.labels["workspace"] + ":" + key
        volume = await self.volume(disk_key)
        if volume.active_attachments:
            raise ComputerProviderError("The durable disk is still attached; inspect the previous computer")
        image = self.config.tenki_desktop_image
        image_option = {"snapshot_id": image[9:]} if image.startswith("snapshot:") else {"image": image}
        remote = await checked_sdk(self.sdk.create(name=options.name, wait=False,
            cpu_cores=2, memory_mb=4096, disk_size_gb=20, max_duration=options.timeout,
            pause_retention=self.config.tenki_pause_retention, allow_inbound=True, allow_outbound=True,
            metadata={**options.labels, "computer_name": options.name, "data_volume": volume.id},
            volumes=[{"volume_id": volume.id, "mount_path": WORKSPACE}], **image_option))
        session = TenkiDesktopSession(self, remote)
        self.sessions.append(session)
        return session

    async def close(self):
        for session in self.sessions:
            # AsyncSandbox.close() TERMINATES. Only release local SDK transports here.
            session.sdk.detach()
            await session.sdk._reset_data_plane()  # Pinned SDK 1.4.1; covered by detach contract test.
        self.sessions.clear()
        await self.sdk.close()


class TenkiDesktopSession(DesktopSession):
    """Reuse shell-quoted Linux input helpers; no E2B resource is constructed."""
    def __init__(self, client, remote):
        super().__init__(client, remote.id, dict(remote.info.metadata),
                         "running" if remote.state == "RUNNING" else "paused", remote)
        self.key = key_from_labels(self.labels)
        self.host_key = self.key
        self.display_number = 0
        self.desktop_key = self.key
        # A transitional VM has not confirmed a durable pause yet. Cleanup must
        # wait for PAUSED rather than treating every non-running state as stopped.
        self.state = {"RUNNING": "started", "PAUSED": "stopped"}.get(remote.state, "pausing")

    async def connection(self):
        return self.sdk

    async def start(self, *, timeout=60):
        async with asyncio.timeout(self.client.config.tenki_lifecycle_timeout):
            await checked_sdk(self.sdk.refresh())
            if self.sdk.state == "PAUSING":
                await checked_sdk(self.sdk.wait_paused(self.client.config.tenki_lifecycle_timeout))
            if self.sdk.state == "PAUSED":
                await checked_sdk(self.sdk.resume())
            await checked_sdk(self.sdk.wait_ready(self.client.config.tenki_lifecycle_timeout))
            volume_id = self.labels.get("data_volume")
            with db() as session:
                disk = session.get(ComputerDisk, self.labels.get("workspace", "") + ":" + self.key)
            if not disk or disk.volume_id != volume_id or disk.state != "ready" or not any(m.volume_id == volume_id and m.mount_path == WORKSPACE
                    and not m.read_only for m in self.sdk.info.volume_mounts):
                raise ComputerProviderError("The persistent data disk is not mounted with verified ownership")
            await self.keepalive(self.client.config.tenki_timeout)
            await self.prepare_home()
            self.state = "started"

    async def stop(self, *, timeout=180):
        async with asyncio.timeout(timeout):
            await checked_sdk(self.sdk.refresh())
            if self.sdk.state == "PAUSING":
                await checked_sdk(self.sdk.wait_paused(timeout))
            elif self.sdk.state != "PAUSED":
                # Catch new files created directly in HOME since the last attach.
                await self.prepare_home()
                await checked_sdk(self.sdk.pause())
                await checked_sdk(self.sdk.wait_paused(timeout))
            if self.sdk.state != "PAUSED":
                raise ComputerProviderError("Pause was not confirmed")
            self.state = "stopped"

    async def keepalive(self, seconds):
        deadline = self.sdk.info.timeout_at
        if deadline is None:
            raise ComputerProviderError("The computer runtime deadline is unavailable")
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        extra = (utcnow() + timedelta(seconds=max(seconds + 60, self.client.config.tenki_timeout)) - deadline).total_seconds()
        if extra > 1:
            await checked_sdk(self.sdk.extend(extra))

    async def exec(self, command: str, *, timeout=30):
        if self.display_number:
            from app.computer.desktop_session import profile_root
            command = (f"export DISPLAY=:{self.display_number} XDG_CONFIG_HOME=" +
                       shlex.quote(profile_root(self.key) + "/config") + "; " + command)
        result = await checked_sdk(self.sdk.exec("bash", "-c", command, cwd=WORKSPACE, timeout=timeout))
        return SimpleNamespace(exit_code=result.exit_code if result.exit_code is not None else 1,
                               result=result.stdout_text, stderr=result.stderr_text)

    async def prepare_home(self):
        fields = (("workspace", "scope", "user", "host_key") if self.labels.get("scope") == "user" else
                  ("workspace", "scope") if self.labels.get("scope") == "workspace" else ("workspace", "agent", "channel"))
        owner = json.dumps({k: self.labels.get(k) for k in fields}, sort_keys=True)
        script = home_script(owner=owner, vm_id=self.id, home="/home/tenki", root=WORKSPACE)
        # The volume mount can initially be root-owned; set only its root ownership.
        await self.checked("sudo chown tenki:tenki " + WORKSPACE)
        await self.checked("python3 -c " + shlex.quote(script), timeout=120)

    async def desktop(self, **kwargs):
        if self.display_number:
            from app.computer.desktop_session import setup_script
            await self.checked(setup_script(self.display_number, self.key), timeout=90)
            for _ in range(60):
                check = await self.exec(f"DISPLAY=:{self.display_number} xprop -root _NET_SUPPORTING_WM_CHECK | grep -q 'window id'", timeout=5)
                if check.exit_code == 0:
                    return
                await asyncio.sleep(.25)
            raise ComputerProviderError("The agent desktop did not become ready")
        await self.checked("sudo systemctl start band-desktop band-web-control band-web-watch")
        for _ in range(60):
            if not (await self.exec("DISPLAY=:0 xdpyinfo >/dev/null 2>&1 && pgrep -x xfdesktop >/dev/null", timeout=5)).exit_code:
                return
            await asyncio.sleep(.25)
        raise ComputerProviderError("Desktop did not become ready")

    async def open_browser(self, url):
        if self.display_number:
            from app.computer.desktop_session import profile_root
            unit = "band-browser-" + uuid4().hex
            await self.checked("sudo systemd-run --quiet --collect --unit=" + unit +
                " --property=User=tenki --setenv=HOME=/home/tenki --setenv=DISPLAY=:" + str(self.display_number) +
                " -- /usr/bin/google-chrome --no-first-run --restore-last-session --disable-quic "
                "--disk-cache-size=33554432 --media-cache-size=16777216 --user-data-dir=" +
                profile_root(self.key) + "/browser " + shlex.quote(url))
            return
        unit = "band-browser-" + uuid4().hex
        await self.checked("sudo systemd-run --quiet --collect --unit=" + unit +
            " --property=User=tenki --setenv=HOME=/home/tenki --setenv=DISPLAY=:0 -- "
            "/usr/bin/google-chrome --no-first-run --restore-last-session --disable-quic --user-data-dir=" +
            WORKSPACE + "/.config/band-browser " + shlex.quote(url))

    async def get_work_dir(self):
        return WORKSPACE

    def select_desktop(self, desktop):
        self.display_number, self.key = desktop.display, desktop.key

    def path(self, path):
        return posixpath.normpath(path if path.startswith("/") else WORKSPACE + "/" + path)

    async def write_file(self, content, path, **kwargs):
        data = content.encode() if isinstance(content, str) else content
        if len(data) > TRANSFER_LIMIT:
            raise ComputerProviderError("File exceeds transfer limit")
        path = self.path(path)
        temporary = "/home/tenki/.band-write-" + uuid4().hex
        await checked_sdk(self.sdk.fs.write_stream(temporary, [data], sync=True))
        staged = posixpath.join(posixpath.dirname(path), ".band-" + uuid4().hex)
        try:
            # Stage beside destination before atomic replacement, including on mounted disks.
            await self.checked("cp -- " + shlex.quote(temporary) + " " + shlex.quote(staged) +
                " && mv -f -- " + shlex.quote(staged) + " " + shlex.quote(path))
        finally:
            await self.exec("rm -f -- " + shlex.quote(staged))
            await checked_sdk(self.sdk.fs.remove(temporary))

    async def read_file(self, path, timeout=120):
        path = self.path(path)
        temporary = "/home/tenki/.band-read-" + uuid4().hex
        # Stream at most TRANSFER_LIMIT+1 guest bytes, even if the source grows after stat.
        script = f"f=open({path!r},'rb'); o=open({temporary!r},'wb'); o.write(f.read({TRANSFER_LIMIT + 1})); o.close(); f.close()"
        await self.checked("python3 -c " + shlex.quote(script), timeout=timeout)
        content = bytearray()
        try:
            async for chunk in self.sdk.fs.read_stream(temporary):
                content.extend(chunk)
                if len(content) > TRANSFER_LIMIT:
                    raise ComputerProviderError("Download exceeds limit")
            return bytes(content)
        finally:
            await checked_sdk(self.sdk.fs.remove(temporary))

    async def list_files(self, path, **kwargs):
        path = self.path(path)
        script = f'''import os,json,stat
out=[]
with os.scandir({path!r}) as entries:
 for e in entries:
  if len(out)>=2000: break
  s=e.stat();out.append(dict(name=e.name,path=e.path,is_dir=stat.S_ISDIR(s.st_mode),size=s.st_size,modified_at=None))
print(json.dumps(out))'''
        return [SimpleNamespace(**entry) for entry in json.loads(await self.checked("python3 -c " + shlex.quote(script)))]

    async def file_info(self, path, **kwargs):
        path = self.path(path)
        script = f"import os,json,stat; s=os.stat({path!r}); print(json.dumps(dict(is_dir=stat.S_ISDIR(s.st_mode),size=s.st_size)))"
        return SimpleNamespace(**json.loads(await self.checked("python3 -c " + shlex.quote(script))))

    async def make_dir(self, path, mode="755", **kwargs):
        await self.checked("mkdir -p -- " + shlex.quote(self.path(path)))

    async def screenshot(self, options=None, **kwargs):
        path = f"/tmp/band-observation-{self.display_number}.png"
        await self.checked(f"DISPLAY=:{self.display_number} scrot --overwrite --pointer " + path)
        data = await self.read_file(path)
        return SimpleNamespace(screenshot=await asyncio.to_thread(jpeg_base64, data))

    async def prepare_app_connection(self, *, seconds: int, port=6080):
        await self.keepalive(seconds)
        from app.computer.user_desktop import ports
        desktop_ports = set(ports(self.display_number)) if self.display_number else {6080, 6081}
        gateway_key = self.host_key
        with db() as session:
            gateway = session.get(ComputerGateway, gateway_key)
            if not gateway or gateway.sandbox_id != self.id:
                secret = secrets.token_hex(32)
                # Another process may create the gateway first; keep its secret for the same VM.
                insert_ignore(session, ComputerGateway, ["key"], key=gateway_key, sandbox_id=self.id,
                              secret_enc=encrypt_secret(secret))
                session.commit()
                gateway = session.get(ComputerGateway, gateway_key)
                if gateway.sandbox_id != self.id:
                    gateway.sandbox_id, gateway.secret_enc = self.id, encrypt_secret(secret)
                    session.add(gateway)
                    session.commit()
                else:
                    secret = decrypt_secret(gateway.secret_enc)
            else:
                secret = decrypt_secret(gateway.secret_enc)
        # Merge port grants in a guest lock so concurrent preview renewals do not revoke one another.
        payload = "/home/tenki/.band-gateway-" + uuid4().hex
        await checked_sdk(self.sdk.fs.write_text(payload, json.dumps({"secret": secret, "ports": sorted({port, *desktop_ports})})))
        script = '''import json,pathlib,re,sys
p=pathlib.Path(sys.argv[1]);new=json.loads(p.read_text());p.unlink()
assert re.fullmatch('[a-f0-9]{64}',new['secret'])
state=pathlib.Path('/home/tenki/.band-gateway.json')
old=json.loads(state.read_text()) if state.exists() else {}
ports=set(old.get('ports',[]))|set(new['ports'])
assert all(type(p) is int and 1024<=p<=49151 for p in ports)
new['ports']=sorted(ports);state.write_text(json.dumps(new));state.chmod(0o600)
config='map_hash_bucket_size 128;\\nmap $http_x_band_ingress $band_auth { default 0; "'+new['secret']+'" 1; }\\n'
config+='map $http_x_band_port $band_port { default ""; '+''.join('"'+str(p)+'" '+str(p)+'; ' for p in sorted(ports))+'}\\n'
config+='map $http_upgrade $band_upgrade { default upgrade; "" close; }\\n'
config+='server { listen 6099; server_tokens off; if ($band_auth = 0) { return 403; } if ($band_port = "") { return 403; } location / { proxy_pass http://127.0.0.1:$band_port; proxy_http_version 1.1; proxy_set_header Host "localhost:$band_port"; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection $band_upgrade; proxy_set_header X-Band-Ingress ""; proxy_set_header X-Band-Port ""; proxy_read_timeout 900s; } }\\n'
pathlib.Path('/home/tenki/.band-nginx.conf').write_text(config)
'''
        await self.checked("flock --wait 30 /home/tenki/.band-gateway.lock sh -c " + shlex.quote(
            "python3 -c " + shlex.quote(script) + " " + shlex.quote(payload) +
            " && sudo install -m 600 /home/tenki/.band-nginx.conf /etc/nginx/conf.d/band.conf && sudo nginx -t && sudo systemctl reload nginx"), timeout=45)
        ttl = max(seconds + 60, self.client.config.tenki_timeout)
        exposed = await checked_sdk(self.sdk.expose_port(6099, ttl=ttl))
        parsed = urlsplit(exposed.url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ComputerProviderError("Invalid private gateway endpoint")
        with db() as session:
            for allowed in {port, *desktop_ports}:
                session.merge(ComputerEndpoint(key=self.key, port=allowed, sandbox_id=self.id,
                    endpoint_enc=encrypt_secret(json.dumps({"origin": exposed.url.rstrip("/"), "secret": secret})),
                    expires_at=utcnow() + timedelta(seconds=ttl)))
            session.commit()

    async def create_signed_preview_url(self, port, *, expires_in_seconds, task_id, user_id):
        from app.computer.viewer import signed_viewer_url
        if self.display_number:
            from app.computer.user_desktop import ports
            port = ports(self.display_number)[0]
        await self.prepare_app_connection(seconds=expires_in_seconds, port=port)
        return SimpleNamespace(url=signed_viewer_url(task_id, user_id, self.id))
