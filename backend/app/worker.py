"""Process entry: uv run python -m app.worker"""
from app.tasks.worker import work

if __name__ == "__main__":
    import asyncio
    import logging

    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(work())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
