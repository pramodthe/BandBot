"""Canonical chunks, local BM25 and account-scoped OpenAI indexes."""
import asyncio
from collections import Counter
import math
import re
from sqlmodel import select
from app.persistence.models import db, Document
from app.providers.models import DocumentProviderIndex, ProviderDocumentCollection, ServiceChange
from app.providers.management import binding_hash, apply_selection
from app.providers.contracts import ProviderError
from app.providers.openai_resources import resource_scope

_index_lock = asyncio.Lock()

def index_lock():
    return _index_lock

def tokens(text):
    return re.findall(r"\w+", text.casefold())

CHUNK_SIZE = 2400
CHUNK_STRIDE = 2200

def _chunks(docs):
    """Split each document unit into overlapping windows for ranking."""
    for doc in docs:
        for unit in doc.units:
            text = unit.get("text")
            if not text:
                continue
            for offset in range(0, len(text), CHUNK_STRIDE):
                location = unit.get("location", "")
                if len(text) > CHUNK_SIZE:
                    location += f" (characters {offset + 1}–{min(offset + CHUNK_SIZE, len(text))})"
                yield doc, {"text": text[offset:offset + CHUNK_SIZE], "location": location}

def local_search(docs, query, limit=8):
    """Rank document chunks with Okapi BM25 (k1=1.5, b=0.75)."""
    chunks = list(_chunks(docs))
    if not chunks:
        return []
    words = [Counter(tokens(unit["text"])) for _, unit in chunks]
    lengths = [sum(counts.values()) for counts in words]
    average = sum(lengths) / len(lengths) or 1
    terms = set(tokens(query))
    frequencies = {term: sum(term in counts for counts in words) for term in terms}
    ranked = []
    for (doc, unit), counts, length in zip(chunks, words, lengths):
        score = sum(math.log(1 + (len(chunks) - frequencies[t] + .5) / (frequencies[t] + .5)) *
            counts[t] * 2.5 / (counts[t] + 1.5 * (.25 + .75 * length / average)) for t in terms if counts[t])
        if score > 0:
            ranked.append((score, doc, unit))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [{"document_id": doc.id, "name": doc.name, "text": unit["text"][:12000],
             "location": unit.get("location", ""), "source": f"[source](/api/documents/{doc.id}/download)"}
            for _, doc, unit in ranked[:limit]]

async def search(task, binding, query):
    from app import documents
    with db() as session:
        documents.agent_authorize(session, task)
        docs = [d for d in session.exec(select(Document).where(Document.channel_id == task.channel_id, Document.status == "ready")).all() if d.units]
    from app.providers.connections import resolve
    resolve(binding, "retrieval")
    if binding.get("provider") == "local":
        matches = local_search(docs, query)
    elif binding.get("provider") == "openai":
        digest = binding_hash(binding)
        with db() as session:
            indexes = [session.get(DocumentProviderIndex, (doc.id, digest)) for doc in docs]
            missing = [doc for doc, row in zip(docs, indexes) if row is None or row.status != "ready"]
            for doc, row in zip(docs, indexes):
                if row is None:
                    session.add(DocumentProviderIndex(document_id=doc.id, binding_hash=digest, binding=binding))
            session.commit()
        if missing:
            raise ProviderError("retrieval", "indexing", connection_id=binding["connection_id"],
                detail="The selected document index is still being prepared. Existing files remain readable. Retry after processing finishes.")
        stores = {r.store_id for r in indexes if r and r.store_id}
        matches = []
        with resource_scope(binding):
            async with documents.client() as sdk:
                for store in stores:
                    response = await sdk.vector_stores.search(vector_store_id=store, query=query[:4000], max_num_results=8)
                    valid = {r.index_file_id: r.document_id for r in indexes}
                    for result in response.data:
                        identity = valid.get(result.file_id)
                        if not identity:
                            continue
                        doc = next((d for d in docs if d.id == identity), None)
                        if doc:
                            matches.append({"document_id": doc.id, "name": doc.name,
                                "text": "\n".join(block.text for block in result.content)[:12000],
                                "source": f"[source](/api/documents/{doc.id}/download)"})
    else:
        raise ProviderError("retrieval", "not_configured")
    # Recheck after provider work; access and deletion are not pinned.
    with db() as session:
        documents.agent_authorize(session, task)
        return {"results": [m for m in matches if (doc := session.get(Document, m["document_id"])) and doc.status == "ready"]}

async def index_document(doc, binding):
    from app import documents
    from app.providers.connections import resolve
    resolve(binding, "retrieval")
    digest = binding_hash(binding)
    with db() as session:
        row = session.get(DocumentProviderIndex, (doc.id, digest))
        if row is None:
            row = DocumentProviderIndex(document_id=doc.id, binding_hash=digest, binding=binding)
            session.add(row)
            session.commit()
        if row.status == "ready":
            return
        original_id, index_id, store_id = row.file_id, row.index_file_id, row.store_id
        state = row.status
        intent = row.upload_intent
    if binding.get("provider") == "local":
        with db() as session:
            row = session.get(DocumentProviderIndex, (doc.id, digest))
            row.status = "ready"
            session.add(row)
            session.commit()
        return
    if binding.get("provider") != "openai":
        raise ProviderError("retrieval", "unsupported_provider")
    def save(**values):
        with db() as session:
            row = session.get(DocumentProviderIndex, (doc.id, digest))
            for name, value in values.items():
                setattr(row, name, value)
            session.add(row)
            session.commit()

    def collection():
        # One remote collection per channel and connection revision.
        with db() as session:
            row = session.get(ProviderDocumentCollection, (doc.channel_id, digest))
            if row is None:
                row = ProviderDocumentCollection(channel_id=doc.channel_id, binding_hash=digest, binding=binding)
                session.add(row)
                session.commit()
            return row

    def save_collection(current):
        with db() as session:
            session.merge(current)
            session.commit()

    try:
        with resource_scope(binding):
            async with documents.client() as sdk:
                if state == "uncertain":
                    # Do not repeat an upload whose result was lost. Reconcile by its
                    # deterministic filename, scoped to the original provider account.
                    for kind, existing in (("original", original_id), ("index", index_id)):
                        if existing or kind != intent:
                            continue
                        name = f"band-{doc.id}-{digest}-{kind}" + (".txt" if kind == "index" else "." + doc.name.rsplit(".", 1)[-1])
                        found = [f async for f in sdk.files.list() if f.filename == name]
                        if len(found) > 1:
                            raise ProviderError("retrieval", "ambiguous_upload")
                        if found and kind == "original":
                            original_id = found[0].id
                        elif found:
                            index_id = found[0].id
                    if (intent == "original" and not original_id) or (intent == "index" and not index_id):
                        raise ProviderError("retrieval", "uncertain_upload", detail="An earlier document upload was not confirmed. Inspect its original provider account before retrying.")
                if not original_id:
                    save(status="uploading", upload_intent="original")
                    name = f"band-{doc.id}-{digest}-original." + doc.name.rsplit(".", 1)[-1]
                    uploaded = await sdk.files.create(file=(name, documents.path_for(doc).read_bytes(), doc.media_type), purpose="user_data",
                        extra_headers={"Idempotency-Key": f"band-doc-{doc.id}-{digest}-original"})
                    original_id = uploaded.id
                    save(file_id=original_id)
                if not index_id:
                    save(status="uploading", upload_intent="index")
                    text = "\n\n".join(f"{u['location']}\n{u['text']}" for u in doc.units)
                    uploaded = await sdk.files.create(file=(f"band-{doc.id}-{digest}-index.txt", text.encode(), "text/plain"), purpose="assistants",
                        extra_headers={"Idempotency-Key": f"band-doc-{doc.id}-{digest}-index"})
                    index_id = uploaded.id
                    save(index_file_id=index_id)
                current = collection()
                if not current.store_id:
                    from app.computer.identity import workspace_id
                    name = f"band-{workspace_id()}-channel-{doc.channel_id}-{digest}"
                    if current.state != "new":
                        stores = [s async for s in sdk.vector_stores.list() if s.name == name]
                        if len(stores) != 1:
                            raise ProviderError("retrieval", "uncertain_collection", detail="A document collection allocation was not confirmed. Inspect the original provider before retrying.")
                        current.store_id = stores[0].id
                    else:
                        current.state = "creating"
                        save_collection(current)
                        created = await sdk.vector_stores.create(name=name, extra_headers={"Idempotency-Key": name})
                        current.store_id = created.id
                    current.state = "ready"
                    save_collection(current)
                store_id = current.store_id
                save(file_id=original_id, index_file_id=index_id, store_id=store_id, status="indexing")
                await sdk.vector_stores.files.create_and_poll(vector_store_id=store_id, file_id=index_id,
                    attributes={"document_id": doc.id})
                status = await sdk.vector_stores.files.retrieve(vector_store_id=store_id, file_id=index_id)
                if status.status != "completed":
                    raise ProviderError("retrieval", "index_failed")
        save(status="ready", error="", upload_intent="")
    except Exception:
        save(status="uncertain" if not original_id or not index_id else "error",
             error="Indexing did not finish. Original files and previous indexes are preserved.")
        raise

async def process_change():
    async with index_lock():
        return await _process_change()

async def _process_change():
    with db() as session:
        change = session.exec(select(ServiceChange).where(ServiceChange.status == "building").order_by(ServiceChange.created_at)).first()
        if not change:
            pending = session.exec(select(DocumentProviderIndex).where(DocumentProviderIndex.status == "queued")).first()
            if not pending:
                return False
            doc = session.get(Document, pending.document_id)
            binding = pending.binding
            change = None
        else:
            docs = [d for d in session.exec(select(Document).where(Document.status == "ready")).all() if d.units]
            binding = change.bindings.get("retrieval", {})
    try:
        if change:
            for doc in docs:
                await index_document(doc, binding)
            with db() as session:
                current = session.get(ServiceChange, change.id)
                apply_selection(session, scope=current.scope, expected_version=current.expected_version,
                    bindings=current.bindings, clear_computer_overrides=current.clear_computer_overrides)
                current.status = "applied"
                session.add(current)
                session.commit()
        elif doc and doc.status == "ready":
            await index_document(doc, binding)
        elif not doc or doc.status in {"deleted", "deleting"}:
            with db() as session:
                current = session.get(DocumentProviderIndex, (pending.document_id, pending.binding_hash))
                current.status = "deleted"
                session.add(current)
                session.commit()
    except Exception:
        if not change and pending:
            with db() as session:
                row = session.get(DocumentProviderIndex, (pending.document_id, pending.binding_hash))
                if row.status == "queued":
                    row.status = "error"
                    row.error = "Index needs its original connection. Reconnect and explicitly retry."
                    session.add(row)
                    session.commit()
        if change:
            with db() as session:
                current = session.get(ServiceChange, change.id)
                current.status = "failed"
                current.error = "Index preparation failed or settings changed. Previous settings and indexes remain active."
                session.add(current)
                session.commit()
    return True

async def _delete_remote_files(row):
    """Delete an index row's provider files; files that are already gone are fine."""
    from openai import NotFoundError
    from app import documents
    with resource_scope(row.binding):
        async with documents.client() as sdk:
            for file_id in set(filter(None, [row.file_id, row.index_file_id])):
                try:
                    await sdk.files.delete(file_id)
                except NotFoundError:
                    pass

def _mark_index_deleted(row):
    with db() as session:
        current = session.get(DocumentProviderIndex, (row.document_id, row.binding_hash))
        current.file_id = current.index_file_id = current.store_id = ""
        current.status = "deleted"
        session.add(current)
        session.commit()

async def delete_resources(doc):
    from app import documents
    with db() as session:
        rows = session.exec(select(DocumentProviderIndex).where(DocumentProviderIndex.document_id == doc.id)).all()
    for row in rows:
        if any((row.file_id, row.index_file_id)) and row.binding.get("provider") == "openai":
            await _delete_remote_files(row)
        _mark_index_deleted(row)
    await cleanup_empty_collections(connection_ids={r.binding.get("connection_id") for r in rows}, channel_id=doc.channel_id)
    documents.path_for(doc).unlink(missing_ok=True)
    documents.patch(doc.id, status="deleted", units=[], file_id="", index_file_id="")

async def cleanup_empty_collections(*, connection_ids, channel_id=None):
    from openai import NotFoundError
    from app import documents
    with db() as session:
        collections = session.exec(select(ProviderDocumentCollection).where(ProviderDocumentCollection.store_id != "")).all()
    for collection in collections:
        if collection.binding.get("connection_id") not in connection_ids or channel_id is not None and collection.channel_id != channel_id:
            continue
        with db() as session:
            active = session.exec(select(DocumentProviderIndex).where(DocumentProviderIndex.binding_hash == collection.binding_hash,
                DocumentProviderIndex.store_id == collection.store_id, DocumentProviderIndex.status != "deleted")).first()
        if active:
            continue
        with resource_scope(collection.binding):
            async with documents.client() as sdk:
                try:
                    await sdk.vector_stores.delete(collection.store_id)
                except NotFoundError:
                    pass
        with db() as session:
            current = session.get(ProviderDocumentCollection, (collection.channel_id, collection.binding_hash))
            current.store_id = ""
            current.state = "deleted"
            session.add(current)
            session.commit()

async def release_connection_indexes(identity):
    """Explicitly release unused retired indexes; canonical documents stay readable."""
    from app.providers.management import dependencies
    with db() as session:
        usage = dependencies(session, identity)
        if usage["selections"] or usage["unfinished_tasks"] or usage["document_jobs"]:
            raise ValueError("Reassign services and finish existing work before releasing indexes")
        indexes = [r for r in session.exec(select(DocumentProviderIndex)).all() if r.binding.get("connection_id") == identity]
    for row in indexes:
        if row.status in {"uploading", "uncertain"}:
            raise ValueError("Reconcile uncertain uploads in their original account before cleanup")
        if row.binding.get("provider") == "openai" and (row.file_id or row.index_file_id):
            await _delete_remote_files(row)
        _mark_index_deleted(row)
    await cleanup_empty_collections(connection_ids={identity})
