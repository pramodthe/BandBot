"""Native model adapters with a verified common agent contract."""
import base64
import io
from PIL import Image
from langchain_core.messages import HumanMessage, ToolMessage
from app.providers.contracts import ProviderError
from app.providers.connections import resolve

def build_model(connection, model: str):
    key = connection.secret.get("api_key", "")
    if connection.provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, api_key=key, base_url="https://api.openai.com/v1", use_responses_api=True, output_version="responses/v1",
                          store=False, max_tokens=12000, include=["reasoning.encrypted_content"], timeout=90, max_retries=0)
    if connection.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, api_key=key, base_url="https://api.anthropic.com", max_tokens=12000, timeout=90, max_retries=0)
    if connection.provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(model=model, google_api_key=key, vertexai=False, base_url="https://generativelanguage.googleapis.com", max_output_tokens=12000,
                                      timeout=90, max_retries=0)
    if connection.provider == "compatible":
        from langchain_openai import ChatOpenAI
        from app.providers.endpoint import http_client
        from app.providers.context import track
        return ChatOpenAI(model=model, api_key=key or "local-endpoint", base_url=connection.config["base_url"],
            http_async_client=track(http_client(connection.config["base_url"])), use_responses_api=False,
            max_tokens=min(12000, connection.config["context_window"] // 4), timeout=90, max_retries=0, stream_usage=False,
            profile={"max_input_tokens": connection.config["context_window"], "tool_calling": True, "image_inputs": True})
    raise ProviderError("llm", "unsupported_provider", connection_id=connection.id)

def for_binding(binding: dict, service="llm"):
    if binding.get("unresolved"):
        raise ProviderError(service, "legacy_checkpoint", detail="This saved task needs an explicit provider mapping or a new task. Its progress has been preserved.")
    connection = resolve(binding, service)
    try:
        model = build_model(connection, binding["model"])
    except (ValueError, TypeError, KeyError):
        raise ProviderError(service, "configuration", connection_id=connection.id,
            detail="The saved model or endpoint configuration needs administrator attention. Current endpoint restrictions still apply.") from None
    from app.providers.context import current_task_id
    if current_task_id():
        from app.providers.model_dispatch import DurableModel
        return DurableModel(inner=model, binding=binding, task_id=current_task_id(), service=service, profile=getattr(model, "profile", None))
    return model

async def verify_models(connection, models: list[str]):
    if not models:
        raise ValueError("Enter at least one model to verify")
    picture = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(picture, "PNG")
    image = "data:image/png;base64," + base64.b64encode(picture.getvalue()).decode()
    tool = {"name": "check_color", "description": "Report the color of the supplied image.",
            "parameters": {"type": "object", "properties": {"color": {"type": "string"}}, "required": ["color"], "additionalProperties": False}}
    verified = {}
    for name in models:
        model = build_model(connection, name)
        first = HumanMessage(content=[{"type": "text", "text": "Inspect the image and call check_color with its color. Do not answer without using the tool."},
                                     {"type": "image_url", "image_url": {"url": image}}])
        try:
            bound = model.bind_tools([tool])
            response = await bound.ainvoke([first])
            calls = response.tool_calls
            if len(calls) != 1 or calls[0]["name"] != "check_color" or "red" not in str(calls[0]["args"].get("color", "")).lower():
                raise ValueError("Image/tool check failed")
            followup = await bound.ainvoke([first, response,
                ToolMessage(content="The color was verified. Return the word VERIFIED.", tool_call_id=calls[0]["id"])])
            from app.providers.document_analysis import answer_text
            if ("VERIFIED" not in answer_text(followup).upper() or followup.tool_calls or
                    any(not getattr(r, "usage_metadata", None) or r.usage_metadata.get("input_tokens", 0) <= 0 for r in (response, followup))):
                raise ValueError("Tool continuation or usage reporting failed")
            verified[name] = {"tools": True, "vision": True, "usage": True}
        finally:
            client = getattr(model, "http_async_client", None)
            if client is not None:
                await client.aclose()
    return verified

def supports_hosted(binding: dict):
    if binding.get("provider") != "openai" or binding.get("connected") or binding.get("disabled") or binding.get("hosted_code") is False:
        return False
    if binding.get("legacy"):
        return True
    connection = resolve(binding, "llm")
    return bool(connection.verification.get("legacy") or connection.verification.get("models", {}).get(binding.get("model"), {}).get("hosted_code"))
