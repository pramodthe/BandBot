"""Focused image-model generation, without a computer or code-drawing fallback."""
from langchain.agents.middleware import ModelCallLimitMiddleware


def image(by_name: dict) -> dict:
    return {
        "name": "image",
        "description": "Generate requested photos, illustrations and art with a real image model, saved as PNGs.",
        "system_prompt": (
            "Complete the human's image request using generate_image. Write a clear visual prompt with subject, "
            "composition and style. Generate one image unless more were explicitly requested. Return the saved "
            "document link and concise description. Do not draw SVG, use Python, or boot a computer as a substitute. "
            "On failure, report the limitation honestly and never retry uncertain paid calls. Only generate requested assets."),
        "middleware": [ModelCallLimitMiddleware(run_limit=4, exit_behavior="error")],
        "tools": [by_name["generate_image"]],
    }
