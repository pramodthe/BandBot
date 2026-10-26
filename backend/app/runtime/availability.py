"""Disabled optional services are tool limitations, not failed model connections."""
import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from app.providers.contracts import ProviderError


OPTIONAL_SERVICES = {
    "computer": "Cloud computers",
    "research": "Web research API",
    "images": "Image generation",
    "retrieval": "Document search",
    "document_analysis": "Document analysis",
    "analysis": "Document analysis",
    "integrations": "App integrations",
}


def computer_enabled(bindings: dict) -> bool:
    binding = bindings.get("computer", {})
    return bool(binding) and not binding.get("disabled") and bool(binding.get("connection_id") or binding.get("legacy"))


def capability_note(bindings: dict, *, hosted_execution: bool) -> str:
    disabled = list(dict.fromkeys(label for service, label in OPTIONAL_SERVICES.items() if bindings.get(service, {}).get("disabled")))
    note = ("Services explicitly disabled for this task: " + ", ".join(disabled) + ". "
            "Do not call tools or delegate work requiring those services. Explain the missing capability and "
            "ask a workspace admin to enable it in Providers & APIs when it is necessary. " if disabled else "")
    if computer_enabled(bindings):
        note += ("A cloud computer is enabled for this task. Its browser and network access are independent of the Web research API. "
                 "When the human asks to use the computer, or web_search/fetch_page are disabled, the parent can delegate "
                 "live browsing and searches to the computer helper using computer_open_browser and computer_action. "
                 "Disabled Web research means those API tools are unavailable; it does not mean the enabled computer cannot browse. "
                 "Using this selected computer does not switch providers or enable another service. "
                 "Observe pages, cite the URLs actually visited, and request approval before purchases or account changes. ")
    if not hosted_execution:
        note += "OpenAI hosted code execution is not enabled for this task. "
    note += ("If a tool reports not_configured, continue only with capabilities actually available. "
             "Do not switch providers, start another paid service, fabricate research, or claim a file was created. "
             "If a requested PDF or other export needs unavailable execution, explain the setup requirement and "
             "offer the report text; never invent a download link. ")
    return note


def unavailable_message(request, error: ProviderError) -> ToolMessage:
    label = OPTIONAL_SERVICES[error.service]
    return ToolMessage(tool_call_id=request.tool_call["id"], name=request.tool_call["name"], status="error",
        content=json.dumps({"error": f"{label} is not configured for this task. A workspace admin can enable it in Providers & APIs. "
                                     "Explain the limitation and continue only with available tools. Do not claim this operation succeeded.",
                            "service": error.service, "code": "not_configured", "retryable": False}))


def is_service_gap(error: ProviderError) -> bool:
    """An optional service missing from the task's bindings, as opposed to a broken connection."""
    return error.service in OPTIONAL_SERVICES and error.code == "not_configured"


class OptionalServicesMiddleware(AgentMiddleware):
    def wrap_tool_call(self, request, handler):
        try:
            return handler(request)
        except ProviderError as error:
            if not is_service_gap(error):
                raise
            return unavailable_message(request, error)

    async def awrap_tool_call(self, request, handler):
        try:
            return await handler(request)
        except ProviderError as error:
            if not is_service_gap(error):
                raise
            return unavailable_message(request, error)
