"""Paid image requests are journaled before dispatch and never blindly replayed."""
import asyncio
import base64
import hashlib
import io
from PIL import Image
from sqlalchemy.exc import IntegrityError
from app.persistence.models import db, Document, TaskDocument, TaskDocumentSource
from app.providers.models import ProviderOperation
from app.providers.connections import resolve
from app.providers.contracts import ProviderError

async def request_image(connection, model, prompt):
    if connection.provider == "openai":
        from openai import AsyncOpenAI
        async with AsyncOpenAI(api_key=connection.secret["api_key"], base_url="https://api.openai.com/v1", max_retries=0, timeout=180) as sdk:
            result = await sdk.images.generate(model=model, prompt=prompt, n=1, size="1024x1024")
            if not result.data or not result.data[0].b64_json:
                raise ValueError("No image bytes returned")
            return base64.b64decode(result.data[0].b64_json), result.usage.model_dump(mode="json") if result.usage else {}
    if connection.provider == "gemini":
        from google import genai
        from google.genai import types
        async with genai.Client(api_key=connection.secret["api_key"], vertexai=False,
                http_options=types.HttpOptions(base_url="https://generativelanguage.googleapis.com", timeout=180000, retry_options=types.HttpRetryOptions(attempts=1))).aio as sdk:
            result = await sdk.models.generate_content(model=model, contents=prompt,
                config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]))
            for part in result.parts or []:
                if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                    return part.inline_data.data, result.usage_metadata.model_dump(mode="json") if result.usage_metadata else {}
        raise ValueError("The model did not return an image")
    raise ProviderError("images", "unsupported_provider")

async def generate(task, binding, prompt, call_id):
    from app import documents
    from app.tasks.usage import over_budget, BudgetExceeded
    operation_id = "image:" + hashlib.sha256((task.id + ":" + call_id).encode()).hexdigest()
    document_id = hashlib.sha256(operation_id.encode()).hexdigest()[:32]
    connection = resolve(binding, "images")
    with db() as session:
        documents.agent_authorize(session, task)
        reason = over_budget(session, task.agent_id, task.channel_id)
        if reason: raise BudgetExceeded(reason)
        existing = session.get(ProviderOperation, operation_id)
        if existing:
            doc = session.get(Document, document_id)
            if existing.status == "completed" and doc and doc.status == "ready":
                return existing.result
            raise ProviderError("images", "uncertain_request", connection_id=connection.id,
                detail="This image request was already dispatched. Its outcome needs review before a separate new request; retrying cannot send it again.")
        session.add(ProviderOperation(id=operation_id, task_id=task.id, service="images", binding=binding, status="running"))
        try: session.commit()
        except IntegrityError:
            raise ProviderError("images", "request_in_progress") from None
    try:
        data, usage = await request_image(connection, binding["model"], prompt[:24000])
        if usage:
            from app.providers.context import usage_scope
            from app.tasks.usage import record_usage
            from langchain_core.messages import AIMessage
            given = int(usage.get("input_tokens") or usage.get("prompt_token_count") or 0)
            output_tokens = int(usage.get("output_tokens") or (usage.get("candidates_token_count") or 0) + (usage.get("thoughts_token_count") or 0))
            with usage_scope(binding, "images"):
                record_usage(task, AIMessage(content="", id=operation_id, response_metadata={"model_name": binding["model"]},
                    usage_metadata={"input_tokens": given, "output_tokens": output_tokens, "total_tokens": given + output_tokens}), binding["model"])
        if len(data) > documents.LIMIT: raise ValueError("Generated image exceeds the document limit")
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            output = io.BytesIO(); image.save(output, format="PNG"); data = output.getvalue()
        artifact = Document(id=document_id, channel_id=task.channel_id, uploaded_by=0,
            name="generated-image-" + document_id[:8] + ".png", media_type="image/png", size=len(data), status="ready")
        path = documents.path_for(artifact)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".pending")
        temporary.write_bytes(data); temporary.replace(path)
        result = {"document_id": document_id, "url": f"/api/documents/{document_id}/download", "name": artifact.name,
                  "provider": binding["provider"], "model": binding["model"], "usage": usage}
        with db() as session:
            documents.agent_authorize(session, task)
            session.add(artifact)
            session.add(TaskDocument(task_id=task.id, document_id=document_id))
            session.add(TaskDocumentSource(task_id=task.id, document_id=document_id))
            row = session.get(ProviderOperation, operation_id); row.status = "completed"; row.result = result
            session.add(row); session.commit()
        return result
    except BaseException as error:
        with db() as session:
            row = session.get(ProviderOperation, operation_id); row.status = "uncertain"
            row.result = {"error": "Image generation was not confirmed. No automatic retry was sent."}
            session.add(row); session.commit()
        if isinstance(error, asyncio.CancelledError): raise
        raise ProviderError("images", "uncertain_request", connection_id=connection.id,
            detail="Image generation was not confirmed. Check the original provider before requesting another paid image.") from None
