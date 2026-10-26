"""Process entry: uv run python -m app.worker"""
from app.tasks.worker import main

if __name__ == "__main__":
    main()
