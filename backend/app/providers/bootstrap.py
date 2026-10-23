"""One-time local import. Never calls providers or creates cloud resources."""
import json
from sqlmodel import select
from app.auth import encrypt_secret
from app.config import settings, MODELS
from app.persistence.models import (AgentTask, AgentProfile, User, ComputerScope, Document, OpenAISettings, db)
from app.providers.models import (ProviderConnection, ProviderRevision, ServiceSelection,
                                  TaskServiceBinding, DocumentServiceBinding)

def bootstrap():
    from filelock import FileLock
    from pathlib import Path
    import hashlib
    path = Path(settings.checkpoint_path).parent / ("providers-" + hashlib.sha256(settings.database_url.encode()).hexdigest()[:16] + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path), timeout=60):
        _bootstrap()

def _bootstrap():
    with db() as session:
        existing_workspace = session.get(ServiceSelection, "workspace")
        if existing_workspace is None:
            saved = session.get(OpenAISettings, 1)
            legacy_installation = bool(saved or session.exec(select(User.id).limit(1)).first()
                or session.exec(select(AgentProfile.id).limit(1)).first() or session.exec(select(AgentTask.id).limit(1)).first())
            connections = [
                ("openai", settings.openai_api_key, {"models": saved.models if saved and saved.models else [m["id"] for m in MODELS],
                    "default_model": saved.default_model if saved and saved.default_model else settings.default_model}, saved.api_key_enc if saved else ""),
                ("tenki", settings.tenki_api_key, {"image": settings.tenki_desktop_image, "sharing": settings.tenki_computer_sharing,
                    "disk_gib": settings.tenki_disk_gib, "timeout": settings.tenki_timeout, "pause_retention": settings.tenki_pause_retention,
                    "computer_namespace": ""}, ""),
                ("e2b", settings.e2b_api_key, {"template": settings.e2b_template, "timeout": settings.e2b_timeout, "computer_namespace": ""}, ""),
                ("exa", settings.exa_api_key, {}, ""), ("composio", settings.composio_api_key, {}, ""),
                ("local", "", {}, ""), ("mcp", "", {}, ""),
            ]
            available = {}
            for provider, key, config, old_enc in connections:
                if not key and not old_enc and provider not in {"local", "mcp"}:
                    continue
                identity = "imported-" + provider
                secret = old_enc if old_enc else encrypt_secret(json.dumps({"api_key": key})) if key else ""
                # Old OpenAISettings stores a raw encrypted key rather than a JSON secret.
                if old_enc:
                    from app.auth import decrypt_secret
                    secret = encrypt_secret(json.dumps({"api_key": decrypt_secret(old_enc)}))
                session.add(ProviderConnection(id=identity, provider=provider, label=f"Existing {provider} connection"))
                session.add(ProviderRevision(connection_id=identity, revision=1, config=config, secret_enc=secret,
                    verification={"legacy": True, "models": {m: {"tools": True, "vision": True} for m in config.get("models", [])}} if legacy_installation or provider in {"local", "mcp"} else {}, origin="imported"))
                available[provider] = {"connection_id": identity, "revision": 1, "provider": provider}
            session.flush()
            from app.providers.registry import SERVICES
            bindings = {service: {"disabled": True} for service in SERVICES}
            bindings.update(retrieval=available["local"], integrations=available["mcp"])
            if legacy_installation and "openai" in available:
                llm = {**available["openai"], "model": connections[0][2]["default_model"]}
                bindings.update(llm=llm, document_analysis=llm, retrieval=available["openai"])
            else:
                bindings["retrieval"] = available["local"]
            if legacy_installation and settings.computer_provider in available:
                bindings["computer"] = available[settings.computer_provider]
            if legacy_installation and "exa" in available:
                bindings["research"] = available["exa"]
            if legacy_installation and "composio" in available:
                bindings["integrations"] = available["composio"]
            session.add(ServiceSelection(scope="workspace", bindings=bindings))
            for scope in session.exec(select(ComputerScope)).all():
                if scope.provider not in available:
                    continue
                owner, channel = scope.key.removeprefix("agent-").rsplit("-channel-", 1)
                session.add(ServiceSelection(scope=f"computer:{owner}:{channel}", bindings={"computer": available[scope.provider]}))
        else:
            bindings = existing_workspace.bindings
            available = {r.provider: {"connection_id": r.id, "revision": 1, "provider": r.provider}
                         for r in session.exec(select(ProviderConnection)).all() if r.id.startswith("imported-")}
        session.flush()
        from app.providers.bindings import snapshot_task
        for task in session.exec(select(AgentTask).outerjoin(TaskServiceBinding, AgentTask.id == TaskServiceBinding.task_id)
                .where(TaskServiceBinding.task_id == None)).all():
            row = snapshot_task(session, task, legacy=True)
            from app.persistence.models import TaskComputerBinding
            computer = session.get(TaskComputerBinding, task.id)
            if computer:
                row.bindings = {**row.bindings, "computer": available.get(computer.provider, {"legacy": True, "provider": computer.provider})}
        for document in session.exec(select(Document)).all():
            if not session.get(DocumentServiceBinding, document.id):
                original = dict(bindings)
                if document.file_id or document.index_file_id:
                    original["retrieval"] = available.get("openai", {"unresolved": True, "provider": "openai"})
                session.add(DocumentServiceBinding(document_id=document.id, bindings=original))
            if (document.file_id or document.index_file_id) and "openai" in available:
                from app.providers.models import DocumentProviderIndex, ProviderDocumentCollection
                from app.persistence.models import DocumentCollection
                from app.providers.management import binding_hash
                binding = available["openai"]
                digest = binding_hash(binding)
                collection = session.get(DocumentCollection, document.channel_id)
                if not session.get(DocumentProviderIndex, (document.id, digest)):
                    session.add(DocumentProviderIndex(document_id=document.id, binding_hash=digest, binding=binding,
                        status="ready" if document.status == "ready" else "error", file_id=document.file_id,
                        index_file_id=document.index_file_id, store_id=collection.vector_store_id if collection else ""))
                if collection and collection.vector_store_id and not session.get(ProviderDocumentCollection, (document.channel_id, digest)):
                    session.add(ProviderDocumentCollection(channel_id=document.channel_id, binding_hash=digest,
                        binding=binding, store_id=collection.vector_store_id, state="ready"))
                    session.flush()
        session.commit()
