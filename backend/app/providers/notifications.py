"""API-side consumer for settings activated by the separate worker."""
import asyncio
import logging
from sqlmodel import select
from app.persistence.models import db, AgentProfile
from app.providers.models import ServiceSelection

logger = logging.getLogger(__name__)

async def watch_settings():
    from app.workspace.service import service
    from app.workspace.events import bus
    previous = None
    while True:
        try:
            with db() as session:
                current = tuple(sorted((row.scope, row.version) for row in session.exec(select(ServiceSelection)).all()))
                profiles = session.exec(select(AgentProfile)).all()
            if previous is not None and current != previous:
                await bus.publish({"type": "provider_settings_changed"})
                # Listener identity is unchanged; only start listeners that could
                # not start before optional provider setup was complete.
                for profile in profiles:
                    status = service.agents.status.get(profile.id, {}).get("state")
                    if status not in {"online", "starting"}:
                        await service.start_agent(profile)
            previous = current
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("Provider settings consumer failed (%s)", type(error).__name__)
        await asyncio.sleep(2)
