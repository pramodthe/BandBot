"""One Agent SDK subscription per user's app connection; execution stays in the worker."""
import asyncio
from band import Agent
from band.core.simple_adapter import SimpleAdapter
from app.config import settings


class PlatformAdapter(SimpleAdapter):
    def __init__(self, service, user_id, identity):
        super().__init__()
        self.service, self.user_id, self.identity = service, user_id, identity

    @property
    def judges_turns(self):
        return False

    async def on_message(self, msg, tools, history, participants_msg, contacts_msg, *, is_session_bootstrap, room_id):
        await self.service.platform_message(self.user_id, self.identity, room_id, msg)


class PlatformListener:
    def __init__(self, service, user_id, identity, key):
        self.service, self.user_id, self.identity = service, user_id, identity
        self.closing = False
        self.agent = Agent.create(adapter=PlatformAdapter(service, user_id, identity), agent_id=identity, api_key=key,
                                  ws_url=settings.band_ws_url, rest_url=settings.band_rest_url)
        self.task = asyncio.create_task(self.run(), name=f"band-platform-{user_id}")

    async def run(self):
        try:
            await self.agent.run(install_signal_handlers=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if not self.closing:
            await self.service.platform_connection_failed(self.user_id, self.identity)

    async def close(self):
        self.closing = True
        self.task.cancel()
        try:
            await self.agent.stop()
        finally:
            await asyncio.gather(self.task, return_exceptions=True)
