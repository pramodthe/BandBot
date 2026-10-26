"""Keep Code Interpreter artifacts durable and Responses history valid after streamed tool calls."""
from __future__ import annotations

from types import SimpleNamespace

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage

from app.openai_provider import client
from app.runtime.tools.documents import save_generated_file


class GeneratedFilesMiddleware(AgentMiddleware):
    def __init__(self, task):
        self.task = task

    async def aafter_model(self, state, runtime):
        message = state["messages"][-1]
        if not isinstance(message, AIMessage) or not isinstance(message.content, list):
            return None
        blocks, annotations = [], {}
        changed = False
        for block in message.content:
            if not isinstance(block, dict):
                blocks.append(block)
                continue
            for annotation in block.get("annotations", []):
                if annotation.get("type") == "container_file_citation":
                    annotations[(annotation["container_id"], annotation["file_id"])] = annotation
            # Streamed image citations can be merged onto the preceding function call by the
            # adapter. They are output-text metadata, never valid function-call input fields.
            if "annotations" in block and block.get("type") not in {"text", "output_text"}:
                block = {k: v for k, v in block.items() if k != "annotations"}
                changed = True
            blocks.append(block)
        downloads = []
        if annotations:
            async with client() as sdk:
                for annotation in annotations.values():
                    if download := await save_generated_file(self.task, sdk, SimpleNamespace(**annotation)):
                        downloads.append(download)
        if downloads:
            blocks.append({"type": "text", "text": "\n\nGenerated files:\n" + "\n".join(
                f"[{d['name']}]({d['url']})" for d in downloads)})
        if changed or downloads:
            return {"messages": [message.model_copy(update={"content": blocks})]}
        return None
