from __future__ import annotations

from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from sqlmodel import Session, select
from sqlalchemy.orm import defer

from app import documents
from app.auth import current_user
from app.persistence.models import Document, User, get_session, utcnow
from app.workspace.audit import record
from app.workspace.events import bus
from app.workspace.permissions import is_admin

router = APIRouter()


def get_document(session, user, document_id):
    doc = session.get(Document, document_id)
    if not doc or doc.status in {"deleting", "deleted"}:
        raise HTTPException(404, "Document not found")
    documents.authorize(session, doc.channel_id, user.id)
    if doc.status == "staged" and doc.uploaded_by != user.id:
        raise HTTPException(404, "Document not found")
    return doc


@router.post("/api/channels/{channel_id}/documents")
async def upload(channel_id: int, name: str, request: Request, user: User = Depends(current_user),
                 session: Session = Depends(get_session)):
    documents.authorize(session, channel_id, user.id)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > documents.LIMIT:
            raise HTTPException(413, "Maximum file size is 25 MiB")
    name, media = documents.validate_file(name, bytes(data))
    doc = Document(id=uuid4().hex, channel_id=channel_id, uploaded_by=user.id, name=name, media_type=media, size=len(data))
    file_written = documents.save_original(session, doc, bytes(data))
    try:
        session.add(doc)
        record("document_uploaded", actor_type="human", actor_id=user.id, actor_name=user.display_name,
               channel_id=channel_id, detail={"document_id": doc.id, "size": doc.size}, session=session)
        session.commit()
    except Exception:
        if file_written:
            documents.path_for(doc).unlink(missing_ok=True)
        raise
    return documents.summary(doc)


@router.get("/api/channels/{channel_id}/documents")
def list_documents(channel_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    documents.authorize(session, channel_id, user.id)
    rows = session.exec(select(Document).where(Document.channel_id == channel_id,
                        Document.status.notin_(["staged", "deleting", "deleted"]))
                        .options(defer(Document.units))
                        .order_by(Document.created_at.desc()).limit(500)).all()
    return [documents.summary(d) for d in rows]


@router.get("/api/documents/{document_id}")
def detail(document_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    doc = get_document(session, user, document_id)
    return {**documents.summary(doc), "preview": "\n\n".join(f"{u['location']}\n{u['text']}" for u in doc.units)[:30000]}


@router.get("/api/documents/{document_id}/download")
def download(document_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    doc = get_document(session, user, document_id)
    if documents.database_files():
        try:
            payload = documents.read_original(doc)
        except FileNotFoundError:
            raise HTTPException(404, "Original file is unavailable") from None
        return Response(content=payload, media_type=doc.media_type, headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(doc.name)}",
            "X-Content-Type-Options": "nosniff",
        })
    target = documents.path_for(doc)
    if not target.exists():
        raise HTTPException(404, "Original file is unavailable")
    return FileResponse(target, filename=doc.name, media_type=doc.media_type, headers={"X-Content-Type-Options": "nosniff"})


@router.post("/api/documents/{document_id}/retry")
def retry(document_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    doc = get_document(session, user, document_id)
    if doc.uploaded_by != user.id and not is_admin(session, user.id):
        raise HTTPException(403, "Only the uploader or an admin can retry processing")
    if doc.status != "error":
        raise HTTPException(409, "Only failed processing can be retried")
    doc.status, doc.error, doc.updated_at = "queued", "", utcnow()
    session.add(doc)
    session.commit()
    return documents.summary(doc)


@router.delete("/api/documents/{document_id}")
async def remove(document_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    doc = get_document(session, user, document_id)
    if doc.uploaded_by != user.id and not is_admin(session, user.id):
        raise HTTPException(403, "Only the uploader or an admin can remove this document")
    doc.status, doc.updated_at = "deleting", utcnow()
    session.add(doc)
    record("document_removed", actor_type="human", actor_id=user.id, actor_name=user.display_name,
           channel_id=doc.channel_id, detail={"document_id": doc.id}, session=session)
    session.commit()
    await bus.publish({"type": "document_removed", "channel_id": doc.channel_id, "document_id": doc.id})
    return {"removed": True}
