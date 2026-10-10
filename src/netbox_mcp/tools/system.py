"""Server/connection status tools."""

from __future__ import annotations

from typing import Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from netbox_mcp.config import Settings
from netbox_mcp.deps import get_netbox


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="NetBox status", read_only_hint=True, idempotent_hint=True, open_world_hint=True
        )
    )
    async def netbox_status(ctx: Context) -> dict[str, Any]:
        """Check the connection to NetBox and report its version, the API user, and whether
        write tools are enabled on this MCP server. Use this first if other tools fail."""
        nb = get_netbox(ctx)
        status = await nb.status()
        user = await nb.get("authentication-check/")
        return {
            "netbox_url": nb.base_url,
            "netbox_version": status.get("netbox-full-version") or status.get("netbox-version"),
            "plugins": status.get("plugins") or {},
            "rq_workers_running": status.get("rq-workers-running"),
            "api_user": user.get("username"),
            "writes_enabled": settings.netbox_mcp_enable_writes,
        }
