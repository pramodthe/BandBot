from typing import Annotated, Literal

from langchain_core.tools import tool, InjectedToolCallId
from pydantic import Field

from app.providers.context import selection
from app.providers.images import generate


def image_tools(task):
    @tool
    async def generate_image(
        prompt: Annotated[str, Field(min_length=1, max_length=16000)],
        tool_call_id: Annotated[str, InjectedToolCallId],
        title: Annotated[str, Field(min_length=1, max_length=100)] = "Generated image",
        size: Literal["1024x1024", "1536x1024", "1024x1536"] = "1024x1024",
        quality: Literal["low", "medium", "high"] = "medium",
        transparent_background: bool = False,
    ) -> dict:
        """Generate a requested picture using the configured image service and display its PNG inline.

        Describe the image fully in prompt and provide a short title. Use a real image model for
        photos, art and illustrations; never substitute a Python/SVG drawing or start a computer.
        Custom size, quality and transparent background options require an OpenAI image connection.
        Only generate images requested by the human. Do not retry uncertain paid calls.
        """
        return await generate(task, selection("images", task), prompt, tool_call_id, title=title,
            size=size, quality=quality, transparent_background=transparent_background)
    return [generate_image]
