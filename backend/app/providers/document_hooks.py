from sqlalchemy import event
from sqlalchemy.orm import Session
from app.persistence.models import Document, TaskDocument
from app.providers.models import DocumentServiceBinding
from app.providers.bindings import selected, task_binding

@event.listens_for(Session, "before_flush")
def snapshot_documents(session, _context, _instances):
    pending = {r.document_id for r in session.new if isinstance(r, DocumentServiceBinding)}
    for doc in [r for r in list(session.new) + list(session.dirty) if isinstance(r, Document)]:
        if doc.status == "staged" or doc.id in pending or session.get(DocumentServiceBinding, doc.id):
            continue
        links = [r for r in session.new if isinstance(r, TaskDocument) and r.document_id == doc.id]
        parent = task_binding(session, links[0].task_id) if links else None
        bindings = parent.bindings if parent else selected(session)
        session.add(DocumentServiceBinding(document_id=doc.id, bindings=bindings))
