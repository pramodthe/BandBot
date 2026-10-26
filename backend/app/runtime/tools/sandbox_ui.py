"""Persist model-authored interfaces; execution belongs exclusively to the browser sandbox."""
from __future__ import annotations

import re
from typing import Annotated

from langchain_core.tools import InjectedToolCallId, tool
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.persistence.models import AgentProfile, AgentTask, MessageComponent, SandboxRenderCall, db
from app.runtime.tools.components import SHOWN
from app.runtime.ui_request import custom_ui_requested


class SandboxedUI(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initialHeight: int = Field(ge=240, le=1000, description="Estimated interface height in pixels.")
    placeholderMessages: list[str] = Field(min_length=1, max_length=4)
    css: str = Field(max_length=32000)
    html: str = Field(min_length=1, max_length=64000, description="Body fragment with meaningful initial content.")
    jsFunctions: str = Field(default="", max_length=64000, description="Named declarations; classic JavaScript, no top-level import/await.")
    jsExpressions: str = Field(default="", max_length=16000, description="Small synchronous initialization calls.")
    title: str = Field(default="Interactive answer", min_length=1, max_length=100)
    description: str = Field(default="", max_length=1500)
    tool_call_id: Annotated[str, InjectedToolCallId]

    @field_validator("placeholderMessages")
    @classmethod
    def messages(cls, values):
        if any(not value.strip() or len(value) > 120 for value in values):
            raise ValueError("Use 1–4 short, nonempty placeholder messages of at most 120 characters")
        return values

    @field_validator("html")
    @classmethod
    def fragment(cls, value):
        if not value.strip() or re.search(r"<\s*/?\s*(?:html|head|body|meta|base|iframe|frame|frameset|object|embed)\b", value, re.I):
            raise ValueError("Use a body fragment without document wrappers, policy tags, or embedded frames")
        return value

    @model_validator(mode="after")
    def bounded(self):
        if not self.title.strip() or any("\x00" in value for value in (self.css, self.html, self.jsFunctions, self.jsExpressions)):
            raise ValueError("Provide a nonempty title and text without NUL characters")
        if len(self.model_dump_json().encode("utf-8")) > 128000:
            raise ValueError("An interactive interface must fit within 128 KB")
        return self


def save_draft(task: AgentTask, call_id: str, props: dict) -> dict:
    with db() as session:
        previous = session.get(SandboxRenderCall, (task.id, call_id))
        if previous:
            return {"status": "shown", "component_id": previous.component_id, "note": SHOWN}
        stored = session.get(AgentTask, task.id)
        if stored is None or stored.reply_message_id is not None:
            return {"error": "This reply is finished. A requested revision needs a new task."}
        receipt = SandboxRenderCall(task_id=task.id, tool_call_id=call_id)
        session.add(receipt)
        try:
            # Claim the call first. SQLite serializes writers before reading/updating the draft.
            session.flush()
            component = session.exec(select(MessageComponent).where(
                MessageComponent.task_id == task.id, MessageComponent.kind == "sandbox",
            ).order_by(MessageComponent.id.desc())).first()
            if component is None:
                component = MessageComponent(task_id=task.id, tool_call_id=call_id, kind="sandbox", props=props)
            else:
                component.props = props
            session.add(component)
            session.flush()
            receipt.component_id = component.id
            session.add(receipt)
            session.commit()
            return {"status": "shown", "component_id": component.id, "note": SHOWN}
        except IntegrityError:
            session.rollback()
            previous = session.get(SandboxRenderCall, (task.id, call_id))
            if previous:
                return {"status": "shown", "component_id": previous.component_id, "note": SHOWN}
            raise


def sandbox_ui_tools(task: AgentTask):
    @tool(args_schema=SandboxedUI)
    def generate_sandboxed_ui(initialHeight: int, placeholderMessages: list[str], css: str, html: str,
                             tool_call_id: str, jsFunctions: str = "", jsExpressions: str = "",
                             title: str = "Interactive answer", description: str = "") -> dict:
        """Create a working interactive answer in an isolated browser iframe.

        Only for an explicit current human request for a custom/interactive UI, calculator or simulation.
        Enabled UI skills are optional guidance; they do not authorize generating a widget.
        Use for requested custom calculators, simulations, SVG/3D explanations or interactions beyond native
        present_ui components. Emit initialHeight, placeholderMessages, css, html, jsFunctions,
        jsExpressions in that order. Nothing executes on the worker or API host. The saved result
        appears with the completed reply and can be reopened from Views. Repeated calls revise ONE draft
        for this task, replacing earlier source. Combine multiple sections in that one interface.
        Published replies cannot be mutated. A follow-up revision creates one new reply.
        Use real evidence or clearly labelled examples, working controls, validation and reset.
        HTML is a body fragment. All CSS stays inside this iframe. Named JS declarations and small
        initialization calls are classic scripts; dynamically import libraries inside async functions.
        Available imports: three, three/addons/, d3, chart.js, gsap. No credentials, workspace storage,
        parent DOM, backend fetch, popups or navigation of the workspace. Local controls stay local.
        BandUI.sendPrompt({text}) and BandUI.openLink({url}) only PROPOSE a host-reviewed interaction;
        a human must confirm outside the frame. They do not grant tool or external-action approval.
        For a required answer that pauses this task use ask_user, not a sandbox follow-up.
        """
        args = SandboxedUI(initialHeight=initialHeight, placeholderMessages=placeholderMessages, css=css,
                           html=html, jsFunctions=jsFunctions, jsExpressions=jsExpressions, title=title,
                           description=description, tool_call_id=tool_call_id)
        with db() as session:
            stored = session.get(AgentTask, task.id)
            if stored is None or not custom_ui_requested(stored.prompt):
                return {"error": "Custom UI requires an explicit request. Use text or native present_ui instead."}
            profile = session.get(AgentProfile, task.agent_id)
            if profile is None:
                return {"error": "This agent is no longer available"}
            identity = profile.band_agent_id
        return save_draft(task, tool_call_id, {**args.model_dump(exclude={"tool_call_id"}), "agentIdentity": identity})

    return [generate_sandboxed_ui]
