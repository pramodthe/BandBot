"""Bounded, public recovery reasons; never expose raw SDK errors."""
from app.providers.contracts import ProviderError, failure_code
from app.providers.bindings import bindings_for
from app.providers.models import ProviderBlock
from app.persistence.models import db

def classify(error, task):
    if isinstance(error, ProviderError):
        return error
    module = type(error).__module__
    from app.computer.providers import ComputerProviderError
    computer_error = module.startswith(("e2b", "tenki")) or isinstance(error, ComputerProviderError)
    if not computer_error and not module.startswith(("openai", "anthropic", "google", "httpx", "httpcore")):
        return None
    status = getattr(error, "status_code", None) or getattr(error, "code", None)
    code = failure_code(status, authentication_error=computer_error and "Authentication" in type(error).__name__)
    service = "computer" if computer_error else "llm"
    binding = bindings_for(task).get(service, {})
    return ProviderError(service, code, connection_id=binding.get("connection_id", ""))

def block(task, error):
    with db() as session:
        session.merge(ProviderBlock(task_id=task.id, service=error.service, code=error.code,
                                   connection_id=error.connection_id, detail=error.detail))
        session.commit()
