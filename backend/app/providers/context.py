"""Attempt-local bindings; permissions are deliberately never cached here."""
import logging
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from app.providers.bindings import bindings_for, workspace_bindings
from app.persistence.models import db
from app.providers.connections import resolve

_bindings = ContextVar("provider_bindings", default=None)
_usage = ContextVar("provider_usage_binding", default=None)
_task_id = ContextVar("provider_task_id", default="")

def current_task_id():
    return _task_id.get()

_computer = ContextVar("provider_computer", default=None)

def current_computer():
    return _computer.get()

_resources = ContextVar("provider_resources", default=None)
logger = logging.getLogger(__name__)

def selection(service: str, task=None):
    if task is not None:
        return bindings_for(task).get(service, {})
    current = _bindings.get()
    if current is not None:
        return current.get(service, {"disabled": True})
    with db() as session:
        return workspace_bindings(session).get(service, {})

def connection(service: str, task=None):
    return resolve(selection(service, task), service)

@contextmanager
def usage_scope(binding, service):
    token = _usage.set((binding, service))
    try:
        yield
    finally:
        _usage.reset(token)

def usage_selection():
    return _usage.get()

def track(client):
    resources = _resources.get()
    if resources is not None:
        resources.append(client)
    return client

async def _close_all(clients):
    """Close tracked provider clients newest first; one failure never blocks the rest."""
    for client in reversed(clients):
        try:
            await client.aclose()
        except Exception as exc:
            logger.debug("Provider client close failed (%s)", type(exc).__name__)

@asynccontextmanager
async def task_services(task, computer=None):
    token = _bindings.set(bindings_for(task))
    computer_token = _computer.set(computer)
    task_token = _task_id.set(task.id)
    clients = []
    resources = _resources.set(clients)
    try:
        yield
    finally:
        await _close_all(clients)
        _resources.reset(resources)
        _bindings.reset(token)
        _computer.reset(computer_token)
        _task_id.reset(task_token)

@asynccontextmanager
async def document_services(document_id):
    from app.providers.bindings import document_bindings
    token = _bindings.set(document_bindings(document_id))
    task_token = _task_id.set("document:" + document_id)
    clients = []
    resource_token = _resources.set(clients)
    try:
        yield
    finally:
        await _close_all(clients)
        _resources.reset(resource_token)
        _task_id.reset(task_token)
        _bindings.reset(token)
