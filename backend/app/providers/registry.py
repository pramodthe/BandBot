"""Built-in manifests; provider configuration never loads arbitrary Python."""
from dataclasses import dataclass, asdict

SERVICES = ("llm", "computer", "research", "retrieval", "document_analysis", "images", "integrations")

@dataclass(frozen=True)
class Manifest:
    id: str
    label: str
    services: tuple[str, ...]
    fields: tuple[str, ...] = ()
    secret_required: bool = True
    capabilities: tuple[str, ...] = ()

MANIFESTS = {
    m.id: m for m in [
        Manifest("openai", "OpenAI", ("llm", "document_analysis", "retrieval", "images"), ("models", "image_model"), capabilities=("tools", "vision", "hosted_code")),
        Manifest("anthropic", "Anthropic", ("llm", "document_analysis"), ("models",), capabilities=("tools", "vision")),
        Manifest("gemini", "Google Gemini", ("llm", "document_analysis", "images"), ("models", "image_model"), capabilities=("tools", "vision")),
        Manifest("compatible", "OpenAI-compatible endpoint", ("llm", "document_analysis"), ("base_url", "models", "context_window"), False, ("tools", "vision")),
        Manifest("tenki", "Tenki", ("computer",), ("image", "sharing", "disk_gib", "timeout", "pause_retention"), capabilities=("desktop", "files", "pause", "durable_disk", "workspace_sharing")),
        Manifest("e2b", "E2B", ("computer",), ("template", "timeout"), capabilities=("desktop", "files", "pause")),
        Manifest("exa", "Exa", ("research",)),
        Manifest("tavily", "Tavily", ("research",)),
        Manifest("local", "Local document search", ("retrieval",), (), False),
        Manifest("composio", "Composio", ("integrations",)),
        Manifest("mcp", "Configured MCP servers", ("integrations",), (), False),
    ]
}

def manifest(provider: str) -> Manifest:
    if provider not in MANIFESTS:
        raise ValueError("Unsupported provider")
    return MANIFESTS[provider]

def catalog():
    return [asdict(value) for value in MANIFESTS.values()]
