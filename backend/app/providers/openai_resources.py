"""Scoped OpenAI resource access, separate from the primary agent model."""
from contextlib import contextmanager
from contextvars import ContextVar
from openai import AsyncOpenAI
from app.providers.connections import resolve

_binding = ContextVar("openai_resource_binding", default=None)

@contextmanager
def resource_scope(binding):
    token = _binding.set(binding)
    try:
        yield
    finally:
        _binding.reset(token)

def resource_client():
    binding = _binding.get()
    if binding is None:
        return None
    connection = resolve(binding, "retrieval")
    return AsyncOpenAI(api_key=connection.secret["api_key"], base_url="https://api.openai.com/v1", timeout=90, max_retries=0)
