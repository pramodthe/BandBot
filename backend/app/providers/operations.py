"""Bounded service dispatch with local replay and fail-closed uncertainty."""
import hashlib
import json
from sqlalchemy.exc import IntegrityError
from app.persistence.models import db
from app.providers.models import ProviderOperation
from app.providers.contracts import ProviderError

async def dispatch(task_id, service, binding, payload, operation):
    digest = hashlib.sha256(json.dumps({"binding": binding, "payload": payload}, sort_keys=True).encode()).hexdigest()
    identity = service + ":" + task_id + ":" + digest
    with db() as session:
        row = session.get(ProviderOperation, identity)
        if row:
            if row.status == "completed":
                return row.result
            raise ProviderError(service, "uncertain_request", connection_id=binding["connection_id"],
                detail="A previous service request was not confirmed. Its saved work is preserved. Review the provider before starting a separate request.")
        session.add(ProviderOperation(id=identity, task_id=task_id, service=service, binding=binding, status="running"))
        try:
            session.commit()
        except IntegrityError:
            raise ProviderError(service, "request_in_progress") from None
    try:
        result = await operation()
        with db() as session:
            row = session.get(ProviderOperation, identity)
            row.status = "completed"
            row.result = result
            session.add(row)
            session.commit()
        return result
    except BaseException as error:
        with db() as session:
            row = session.get(ProviderOperation, identity)
            if getattr(error, "confirmed_rejection", False):
                session.delete(row)
            else:
                row.status = "uncertain"
                row.result = {"error": "Response was not confirmed. No automatic retry was sent."}
                session.add(row)
            session.commit()
        raise
