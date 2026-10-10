"""OpenAPI-driven escape hatches: search the spec, inspect definitions, make raw API calls."""

# No `from __future__ import annotations`: tool signatures reference locals (method_desc),
# which FastMCP must be able to resolve when it inspects them.

import json
import logging
import re
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.deps import current_caller, get_netbox, get_previews, get_schema
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.formatting import compact as _compact
from netbox_mcp.formatting import list_result, object_result
from netbox_mcp.preview import Plan, preview_or_apply
from netbox_mcp.schema import SchemaIndex
from netbox_mcp.tools.common import label
from netbox_mcp.tools.writes import delete_effects, field_changes

audit = logging.getLogger("netbox_mcp.audit")

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

# Relative API path: letters, digits, '-', '_', '/', '.'; no '..' segments or query strings.
_PATH_RE = re.compile(r"^/?(api/)?[A-Za-z0-9_\-/.]+$")


_DETAIL_RE = re.compile(r"^/?(?:api/)?(?P<base>[a-z-]+/[a-z0-9-]+/)(?P<id>\d+)/$")


async def _write_plan(
    nb: NetBoxClient, schema: SchemaIndex, method: str, path: str, params: Any, body: Any
) -> Plan:
    """Preview a raw write: the request itself, plus for PATCH/PUT/DELETE on one object its current
    state, the field changes, or the known side effects of deleting it."""
    details: dict[str, Any] = {"method": method, "path": f"/api/{path.lstrip('/')}"}
    if params:
        details["params"] = params
    if body is not None:
        details["body"] = body
    fingerprint: Any = None
    m = _DETAIL_RE.match(path)
    if m and method in ("PATCH", "PUT", "DELETE"):
        current = await nb.get_object(path)
        fingerprint = [current.get("id"), current.get("last_updated")]
        details["object"] = f"{label(current)} (id {current.get('id')})"
        otype = next((t for t in schema.types.values() if t.path == m.group("base")), None)
        if method == "DELETE" and otype is not None:
            effects, blockers = await delete_effects(nb, otype, [current])
            if effects:
                details["side_effects"] = effects
            return Plan(f"DELETE {details['object']}", details, fingerprint, blockers)
        if isinstance(body, dict):
            changes, unchanged = field_changes(current, body)
            details["changes"] = changes
            if unchanged:
                details["unchanged"] = unchanged
    return Plan(f"{method} /api/{path.lstrip('/')}", details, fingerprint)


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
        confirm: Annotated[
            str | None,
            Field(description="Non-GET only: omit to preview; repeat with confirm=<token> to apply."),
        ] = None,
    ) -> ToolResult:
        """Call any NetBox REST endpoint directly. Prefer the dedicated tools; use this for
        endpoints they don't cover (find them with netbox_search_spec). Non-GET calls are preview-first:
        the first call shows what would happen and returns a confirm token."""
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
        nb = get_netbox(ctx)

        if method != "GET":
            args = {"path": path, "method": method, "params": params, "body": body}
            plan = await _write_plan(nb, await get_schema(ctx), method, path, params, body)

            async def apply() -> dict[str, Any]:
                response = await nb.request(method, path, params=params, json=body)
                audit.info("caller=%s api_call %s /api/%s", current_caller(), method, path.lstrip("/"))
                if response is None:
                    return {"response": "no content"}
                return {"response": _compact(response) if compact else response}

            out = await preview_or_apply(
                get_previews(ctx),
                tool="netbox_api_call",
                args=args,
                confirm=confirm,
                caller=current_caller(),
                plan=plan,
                apply=apply,
            )
            return ToolResult(
                content=json.dumps(out, separators=(",", ":"), default=str), structured_content=out
            )

        result = await nb.request(method, path, params=params, json=body)
        if result is None:
            return ToolResult(content=f"{method} /api/{path.lstrip('/')} succeeded (no content).")
        if isinstance(result, dict) and "results" in result and "count" in result:
            offset = int((params or {}).get("offset", 0))
            return list_result(result["results"], count=result["count"], offset=offset, use_compact=compact)
        if isinstance(result, list):
            return list_result(result, count=len(result), offset=0, use_compact=compact)
        return object_result(result, use_compact=compact)
