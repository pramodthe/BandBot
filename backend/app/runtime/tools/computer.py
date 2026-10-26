"""Cloud desktop, shell, and file tools for one task."""
from __future__ import annotations

import logging
from typing import Annotated, Literal

from langchain_core.tools import InjectedToolCallId, tool
from pydantic import Field

from app.computer import ComputerUnavailable, TaskComputer
from app.providers.contracts import ProviderError, failure_code
from app.persistence.models import AgentTask

logger = logging.getLogger(__name__)


def computer_tools(task: AgentTask, computer: TaskComputer):
    async def computer_call(operation):
        try:
            result = await operation()
            if computer.notice:
                # Surface a replaced computer once, alongside the result that triggered it.
                notice, computer.notice = computer.notice, ""
                if isinstance(result, list):
                    return [{"type": "text", "text": notice}, *result]
                return {**result, "notice": notice}
            return result
        except ProviderError:
            raise
        except (ComputerUnavailable, ValueError) as exc:
            return {"error": str(exc)}
        except Exception as exc:
            if type(exc).__module__.startswith(("e2b", "tenki", "httpx", "httpcore")):
                from app.providers.context import selection
                status = getattr(exc, "status_code", None)
                code = failure_code(status, authentication_error="Authentication" in type(exc).__name__)
                raise ProviderError("computer", code, connection_id=selection("computer", task).get("connection_id", "")) from None
            # SDK errors may contain full request headers. Expose only the type.
            logger.warning("Computer tool failed for task %s: %s", task.id, type(exc).__name__)
            return {"error": f"Computer operation failed ({type(exc).__name__}). Its outcome may be uncertain. "
                             "For desktop actions, take a fresh screenshot before deciding what to do next."}

    @tool
    async def run_python(code: str) -> dict:
        """Run Python on the cloud computer (30s limit). Never executes on the host.

        Files persist across calls and later tasks; Python variables do not. Requires configured computer access.
        Starting the computer can take a while: if you have code_interpreter, use it for quick calculations instead.
        """
        return await computer_call(lambda: computer.command(code, python=True))

    @tool
    async def sandbox_exec(command: str) -> dict:
        """Run a shell command inside the task's cloud sandbox, with bounded output and a 30s limit.

        Use for installing packages, running tests, or inspecting sandbox files. Host secrets are not available.
        Long commands should start a background process with redirected output, then poll its log.
        """
        return await computer_call(lambda: computer.command(command))

    @tool
    async def computer_screenshot() -> list | dict:
        """See the cloud desktop. Returns an actual image and screen_id required by computer_action.

        Use the image to choose coordinates. Never guess from an old screenshot. Starts a computer on first use.
        """
        return await computer_call(computer.screenshot)

    @tool
    async def computer_open_browser(url: str) -> list | dict:
        """Open an HTTP(S) URL in the sandbox's graphical browser and see its desktop screenshot.

        Requires the cloud desktop template or a compatible template with Chromium or Firefox.
        Check the image to confirm the page loaded; a launch alone is not a successful visit.
        """
        return await computer_call(lambda: computer.open_browser(url))

    @tool
    async def computer_action(
        screen_id: str, action: Literal["click", "move", "drag", "scroll", "type", "key", "wait"],
        x: Annotated[int, Field(ge=0)] = 0, y: Annotated[int, Field(ge=0)] = 0,
        end_x: Annotated[int, Field(ge=0)] = 0, end_y: Annotated[int, Field(ge=0)] = 0,
        text: Annotated[str, Field(max_length=4000)] = "", key: Annotated[str, Field(max_length=40)] = "",
        modifiers: list[Literal["ctrl", "alt", "shift", "cmd"]] | None = None,
        button: Literal["left", "right", "middle"] = "left", double: bool = False,
        direction: Literal["up", "down"] = "down", amount: Annotated[int, Field(ge=1, le=20)] = 3,
        seconds: Annotated[float, Field(ge=0.1, le=3)] = 1,
    ) -> list | dict:
        """Operate the remote desktop using a fresh screen_id, then observe the resulting screenshot.

        click/move/scroll use x,y; drag also uses end_x,end_y; type uses text; key uses key and modifiers
        (e.g. key='l', modifiers=['ctrl']). wait uses seconds. Each screen_id is valid for one action.
        On an uncertain failure, take a new screenshot instead of repeating an old action.
        """
        return await computer_call(lambda: computer.action(screen_id=screen_id, action=action, x=x, y=y,
            end_x=end_x, end_y=end_y, text=text, key=key, modifiers=modifiers, button=button, double=double,
            direction=direction, amount=amount, seconds=seconds))

    @tool
    async def sandbox_write_file(path: str, content: str) -> dict:
        """Write UTF-8 text to an absolute path in the cloud computer (up to 200000 bytes).

        Deep Agents virtual files are separate. Use this tool to copy code or data onto the actual computer.
        """
        return await computer_call(lambda: computer.write_file(path, content))

    @tool
    async def sandbox_export_file(path: str) -> dict:
        """Export a UTF-8 text file from the cloud computer to downloadable task results (up to 200000 bytes).

        Export final deliverables before finishing. Paused computers retain files until explicitly deleted.
        """
        if getattr(computer, "user_mode", False):
            from app.persistence.models import DirectMessage, db
            from langgraph.types import interrupt
            with db() as session:
                group = session.get(DirectMessage, task.channel_id) is None
            if group:
                decision = interrupt({"kind": "private_computer_export", "action": "Publish a private computer file to this channel",
                    "details": "The computer owner must approve sharing this file's contents with the channel."})
                if not decision.get("approved"):
                    return {"error": "The computer owner did not approve publishing this file"}
        return await computer_call(lambda: computer.export_file(path))

    @tool
    async def publish_app(port: Annotated[int, Field(ge=1024, le=49151)], title: Annotated[str, Field(min_length=1, max_length=100)],
                          tool_call_id: Annotated[str, InjectedToolCallId], path: str = "/") -> dict:
        """Attach a live, interactive server-app preview to your chat reply.

        First start the app in the existing cloud computer as a background server listening on 0.0.0.0.
        Serve frontend and API on this port (or configure a dev-server API proxy). Use relative browser
        API/WebSocket URLs, never sandbox localhost URLs or public tunnels. Keep the process running.
        This registers only the port; it does not start a server or allocate a computer. Verify the app
        in its browser before finishing. Existing computers may need the dev server's allowedHosts to
        include its private gateway host. Users connect and resume the computer privately from the reply.
        """
        from app.computer.app_previews import publish
        return await computer_call(lambda: publish(computer, port=port, title=title, path=path, call_id=tool_call_id))

    return [run_python, sandbox_exec, computer_screenshot, computer_open_browser, computer_action,
            sandbox_write_file, sandbox_export_file, publish_app]
