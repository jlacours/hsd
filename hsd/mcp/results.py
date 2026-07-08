"""Result helpers for MCP tool responses."""

import json

from mcp.types import TextContent, CallToolResult


def ok_result(data) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, indent=2))],
        isError=False,
    )


def error_result(msg: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps({"error": msg}, indent=2))],
        isError=True,
    )
