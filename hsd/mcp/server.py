"""MCP server exposing HSD task lifecycle as tools.

Runs on stdio transport. Each MCP-aware harness connects via its own process.
"""

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, CallToolResult

from hsd.core.db import Database

from hsd.mcp.schemas import INSTRUCTIONS, _get_tools
from hsd.mcp.handlers import _handle_call
from hsd.mcp.results import ok_result, error_result

__all__ = ["make_server", "main", "_handle_call", "error_result", "ok_result"]


def make_server(db: Database | None = None) -> Server:
    if db is None:
        db = Database()
    server = Server("hsd", instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return _get_tools()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> CallToolResult:
        try:
            return await _handle_call(db, name, arguments)
        except Exception as e:
            return error_result(f"Server error: {e}")

    return server


def main() -> None:
    """Entry point for hsd-mcp console script."""
    import asyncio

    async def _run() -> None:
        db = Database()
        server = make_server(db)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
