"""ASGI entry: uvicorn app.main:app"""
from app.api.http import app

__all__ = ["app"]
