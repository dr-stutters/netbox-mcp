"""OpenAPI-driven escape hatches: search the spec, inspect definitions, make raw API calls."""

# No `from __future__ import annotations`: tool signatures reference locals (method_desc),
# which FastMCP must be able to resolve when it inspects them.

import re
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.config import Settings
from netbox_mcp.deps import get_netbox, get_schema
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.formatting import list_result, object_result

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

# Relative API path: letters, digits, '-', '_', '/', '.'; no '..' segments or query strings.
_PATH_RE = re.compile(r"^/?(api/)?[A-Za-z0-9_\-/.]+$")


def register(mcp: FastMCP, settings: Settings) -> None:
    writes = settings.netbox_mcp_enable_writes

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_search_spec(
        ctx: Context,
        query: Annotated[
            str, Field(description="Keywords, e.g. 'cable trace', 'available prefixes', 'vlan group'.")
        ],
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> list[dict[str, Any]]:
        """Search NetBox's OpenAPI spec for object types, API operations and schemas matching keywords.
        Use this to discover endpoints that the dedicated tools don't cover."""
        return (await get_schema(ctx)).search(query, limit)

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_get_definition(
        ctx: Context,
        name: Annotated[
            str,
            Field(
                description="An object type ('dcim.devices'), a schema name ('WritableDeviceRequest'), "
                "or an API path ('/api/ipam/prefixes/{id}/available-ips/')."
            ),
        ],
        section: Annotated[
            Literal["all", "fields", "write", "filters"],
            Field(
                description="For object types: which part to return (fields=read model, write=create/update)."
            ),
        ] = "all",
    ) -> dict[str, Any]:
        """Describe an object type (fields, enum choices, writable/required fields, valid filters,
        extra endpoints), a schema, or the operations at an API path."""
        return (await get_schema(ctx)).definition(name, section)

    method_desc = "HTTP method." if writes else "HTTP method. Only GET is allowed: this server is read-only."

    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=not writes, destructive_hint=writes, idempotent_hint=False, open_world_hint=True
        )
    )
    async def netbox_api_call(
        ctx: Context,
        path: Annotated[
            str, Field(description="API path relative to /api/, e.g. 'dcim/interfaces/12/trace/'.")
        ],
        method: Annotated[
            Literal["GET", "POST", "PUT", "PATCH", "DELETE"], Field(description=method_desc)
        ] = "GET",
        params: Annotated[dict[str, Any] | None, Field(description="Query parameters.")] = None,
        body: Annotated[Any, Field(description="JSON request body for POST/PUT/PATCH/DELETE.")] = None,
        compact: Annotated[
            bool, Field(description="Trim the response (drop urls/empty fields, shrink nested objects).")
        ] = True,
    ) -> ToolResult:
        """Call any NetBox REST endpoint directly. Prefer the dedicated tools; use this for
        endpoints they don't cover (find them with netbox_search_spec)."""
        if method != "GET" and not writes:
            raise InvalidRequest(
                "This server is read-only: only GET is allowed. "
                "Writes require NETBOX_MCP_ENABLE_WRITES=true on the server."
            )
        if not _PATH_RE.match(path) or ".." in path:
            raise InvalidRequest(
                f"Invalid API path {path!r}: use a relative path like 'dcim/devices/1/', no query string."
            )
        if not path.endswith("/"):
            path += "/"

        result = await get_netbox(ctx).request(method, path, params=params, json=body)
        if result is None:
            return ToolResult(content=f"{method} /api/{path.lstrip('/')} succeeded (no content).")
        if isinstance(result, dict) and "results" in result and "count" in result:
            offset = int((params or {}).get("offset", 0))
            return list_result(result["results"], count=result["count"], offset=offset, use_compact=compact)
        if isinstance(result, list):
            return list_result(result, count=len(result), offset=0, use_compact=compact)
        return object_result(result, use_compact=compact)
