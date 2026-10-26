"""One operation at a time across API/worker processes. OS releases locks on crash."""
import asyncio
import hashlib
import sys

from app.config import state_dir
from app.persistence.models import engine


class ComputerLock:
    def __init__(self, key: str, *, timeout: float = 240):
        root = state_dir(engine.url) / "computer-locks"
        self.path = root / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
        self.timeout = timeout
        self.local = asyncio.Lock()
        self.file = None

    async def __aenter__(self):
        await self.local.acquire()
        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            handle.seek(0, 2)
            if not handle.tell():
                handle.write(b"0")
                handle.flush()
            deadline = asyncio.get_running_loop().time() + self.timeout
            while True:
                try:
                    handle.seek(0)
                    if sys.platform == "win32":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.file = handle
                    return self
                except OSError:
                    if asyncio.get_running_loop().time() >= deadline:
                        from app.computer.desktop import ComputerUnavailable
                        raise ComputerUnavailable("The shared computer is busy. Try again shortly.") from None
                    await asyncio.sleep(.05)
        except BaseException:
            if handle is not None:
                handle.close()
            self.local.release()
            raise

    async def __aexit__(self, *_args):
        try:
            self.file.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None
            self.local.release()
