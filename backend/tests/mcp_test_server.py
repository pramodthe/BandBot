"""A tiny stdio MCP server for offline integration tests. Logs each write so tests can count calls."""
import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("tickets")


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def find_ticket(title: str) -> str:
    """Look up a ticket by title."""
    return f"No ticket named {title}"


@server.tool()
def create_ticket(title: str) -> str:
    """Create a ticket."""
    with open(os.environ["MCP_TEST_LOG"], "a", encoding="utf-8") as log:
        log.write(title + "\n")
    return f"Created ticket {title}"


if __name__ == "__main__":
    server.run()
