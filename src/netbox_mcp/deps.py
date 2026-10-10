"""Accessors for objects shared via the server lifespan."""

from __future__ import annotations

from fastmcp import Context

from netbox_mcp.client import NetBoxClient
from netbox_mcp.schema import SchemaIndex, SchemaStore


def get_netbox(ctx: Context) -> NetBoxClient:
    client: NetBoxClient = ctx.lifespan_context["netbox"]
    return client


async def get_schema(ctx: Context) -> SchemaIndex:
    store: SchemaStore = ctx.lifespan_context["schema"]
    return await store.get()
