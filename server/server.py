"""NEVINA MCP server — stdio transport.

Exposes two tools that wrap NVE's NEVINA4 hydrology delineation service.
Run via ``python -m server.server`` (configured in plugin.json).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .nevina_client import NevinaClient
from .tools import (
    COMPARE_SCHEMA,
    DELINEATE_SCHEMA,
    compare_tool,
    delineate_tool,
)

# Log to stderr; stdout is the MCP protocol channel.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s nevina-mcp: %(message)s",
)
logger = logging.getLogger(__name__)

server: Server = Server("nevina")
_client: NevinaClient = NevinaClient()


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="nevina_delineate",
            description=(
                "Delineate the upstream catchment at a point using NVE's "
                "NEVINA4 service and return the full field-parameter set "
                "(area, runoff normals 1991-2020 + 1961-1990, elevation "
                "bands, land cover, climate). Pure NEVINA query — no "
                "engine data involved. Use this to get ground-truth "
                "catchment information for any point in Norway. Input is "
                "WGS84 (lng, lat) by default; UTM33 also accepted."
            ),
            inputSchema=DELINEATE_SCHEMA,
        ),
        Tool(
            name="nevina_compare_to_engine",
            description=(
                "Compare NEVINA's catchment area at a point against the "
                "screening engine's claimed area for the same point. "
                "Returns area ratio, drift percentage and a verdict "
                "(agree / drift_minor / drift_major). This is the "
                "G1-validation workflow: feed engine drainage_area_km2 "
                "or intake_catchment_area_km2 plus the site's intake "
                "coordinates, get a clean verdict on which is right."
            ),
            inputSchema=COMPARE_SCHEMA,
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    arguments = arguments or {}
    logger.info("call_tool name=%s args=%s", name, list(arguments))
    try:
        if name == "nevina_delineate":
            result = await delineate_tool(_client, **arguments)
        elif name == "nevina_compare_to_engine":
            result = await compare_tool(_client, **arguments)
        else:
            return _error_response(f"Unknown tool: {name!r}")
        return [TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception as exc:  # noqa: BLE001 — MCP boundary; report all faults
        logger.exception("Tool %s failed", name)
        return _error_response(f"{type(exc).__name__}: {exc}")


def _error_response(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": message}))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(
            read,
            write,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        # Best-effort cleanup; stdio_server should already have torn down.
        try:
            asyncio.get_event_loop().run_until_complete(_client.aclose())
        except Exception:
            pass
