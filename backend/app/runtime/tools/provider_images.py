from typing import Annotated
from langchain_core.tools import tool, InjectedToolCallId
from app.providers.context import selection
from app.providers.images import generate

def image_tools(task):
    @tool
    async def generate_image(prompt: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> dict:
        """Generate a requested image using the configured image service. Return its durable download URL.

        Only use for images requested by the human. Describe the complete image in prompt.
        A request with an uncertain outcome must not be repeated automatically.
        """
        return await generate(task, selection("images", task), prompt, tool_call_id)
    return [generate_image]
