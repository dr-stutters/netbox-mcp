"""Generic tools that work for any NetBox object type."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.config import Settings
from netbox_mcp.deps import get_netbox, get_schema
from netbox_mcp.formatting import ResponseFormat, list_result, object_result

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

ObjectTypeArg = Annotated[
    str,
    Field(
        description="NetBox object type as 'app.endpoint', e.g. 'dcim.devices', 'ipam.ip-addresses', "
        "'ipam.prefixes', 'virtualization.virtual-machines'. "
        "Unambiguous short names like 'devices' also work."
    ),
]
FilterValue = str | int | float | bool | list[str | int]
FieldsArg = Annotated[
    list[str] | None,
    Field(description="Only return these fields (e.g. ['id','name','status']). Greatly reduces output size."),
]
FormatArg = Annotated[
    ResponseFormat, Field(description="'json' (compact, default) or 'markdown' (human-readable table).")
]


def _params(fields: list[str] | None, brief: bool = False) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if fields:
        params["fields"] = ",".join(fields)
    if brief:
        params["brief"] = "true"
    return params


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def netbox_list_object_types(
        ctx: Context,
        app: Annotated[
            str | None,
            Field(description="Limit to one app: dcim, ipam, circuits, virtualization, tenancy, extras, ..."),
        ] = None,
    ) -> dict[str, dict[str, str]]:
        """List the NetBox object types usable with netbox_list_objects / netbox_get_object,
        grouped by app."""
        schema = await get_schema(ctx)
        grouped: dict[str, dict[str, str]] = {}
        for t in sorted(schema.types.values(), key=lambda t: t.key):
            a, endpoint = t.key.split(".", 1)
            if app and a != app.lower():
                continue
            grouped.setdefault(a, {})[endpoint] = t.description
        return grouped

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_list_objects(
        ctx: Context,
        object_type: ObjectTypeArg,
        filters: Annotated[
            dict[str, FilterValue] | None,
            Field(
                description="NetBox filters, e.g. {'site': 'home', 'status': 'active', 'role_id': 3, "
                "'name__ic': 'core'}. A list value means OR, e.g. {'status': ['active','planned']}. "
                "Use netbox_get_definition(object_type, section='filters') to see valid filter names."
            ),
        ] = None,
        q: Annotated[str | None, Field(description="Free-text search within this object type.")] = None,
        fields: FieldsArg = None,
        brief: Annotated[bool, Field(description="Return minimal representations (id, name, ...).")] = False,
        ordering: Annotated[str | None, Field(description="Sort field, prefix '-' for descending.")] = None,
        limit: Annotated[int, Field(ge=1, le=1000)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
        response_format: FormatArg = "json",
    ) -> ToolResult:
        """List/filter objects of any NetBox type. Returns {count, offset, returned, next_offset?, results}.
        Empty fields are omitted and nested objects are reduced to {id, display}."""
        schema = await get_schema(ctx)
        otype = schema.resolve_type(object_type)
        filters = filters or {}
        schema.check_filters(otype, list(filters))
        params: dict[str, Any] = {**filters, **_params(fields, brief)}
        if q:
            params["q"] = q
        if ordering:
            params["ordering"] = ordering
        results, count = await get_netbox(ctx).list(otype.path, params, limit=limit, offset=offset)
        return list_result(
            results, count=count, offset=offset, fmt=response_format, extra={"object_type": otype.key}
        )

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_get_object(
        ctx: Context,
        object_type: ObjectTypeArg,
        id: Annotated[int, Field(description="Numeric object ID.", ge=1)],
        fields: FieldsArg = None,
        response_format: FormatArg = "json",
    ) -> ToolResult:
        """Get one NetBox object by type and ID, with all its fields."""
        schema = await get_schema(ctx)
        otype = schema.resolve_type(object_type)
        obj = await get_netbox(ctx).get(f"{otype.path}{id}/", _params(fields))
        return object_result(obj, fmt=response_format)
