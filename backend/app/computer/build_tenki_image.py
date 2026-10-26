"""Explicit one-off clean image builder, separate from ordinary agent tasks."""
import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from tenki import AsyncClient
from app.config import settings


async def build(state_file: Path):
    if not settings.tenki_api_key:
        raise ValueError("Configure TENKI_API_KEY first")
    if state_file.exists():
        raise ValueError("Build state exists; inspect it before allocating another builder")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state = {"build_id": uuid4().hex, "status": "creating"}
    def save():
        state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
    save()
    async with AsyncClient(api_key=settings.tenki_api_key, warning_handler=None) as client:
        vm = None
        try:
            vm = await client.create(name="band-image-" + state["build_id"], wait=False,
                cpu_cores=2, memory_mb=4096, disk_size_gb=20, max_duration=1800,
                pause_retention=3600, allow_inbound=False, allow_outbound=True,
                metadata={"purpose": "band-desktop-image", "build_id": state["build_id"]})
            state.update(sandbox_id=vm.id, status="installing")
            save()
            await vm.wait_ready(180)
            await vm.fs.write_bytes("/home/tenki/build.sh", Path(__file__).with_name("tenki_image.sh").read_bytes())
            result = await vm.exec("bash", "/home/tenki/build.sh", timeout=900)
            if not result.ok:
                state.update(status="install_failed", output=result.stderr_text[-4000:])
                save()
                raise RuntimeError("Desktop image build failed; inspect the private build state")
            image = await vm.snapshot(name="band-clean-desktop")
            state.update(snapshot_id=image.id, status="waiting_for_durability")
            save()
            await client.snapshots.wait_durable(image.id, timeout=600)
            state["status"] = "ready"
        finally:
            if vm is not None:
                await vm.close_if_open()  # Only this disposable builder is terminated.
                state["builder_cleanup"] = "terminated"
            save()
    print("TENKI_DESKTOP_IMAGE=snapshot:" + state["snapshot_id"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(build(args.state_file))
    except Exception as exc:
        print("Image build did not complete (" + type(exc).__name__ + "). Inspect its state file before retrying.")
        raise SystemExit(1) from None
