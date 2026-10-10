"""Accessors for objects shared via the server lifespan."""

from __future__ import annotations

from fastmcp import Context
from fastmcp.server.dependencies import get_access_token

from netbox_mcp.client import NetBoxClient
from netbox_mcp.preview import PreviewStore
from netbox_mcp.schema import SchemaIndex, SchemaStore


def get_netbox(ctx: Context) -> NetBoxClient:
    client: NetBoxClient = ctx.lifespan_context["netbox"]
    return client


async def get_schema(ctx: Context) -> SchemaIndex:
    store: SchemaStore = ctx.lifespan_context["schema"]
    return await store.get()


def current_caller() -> str:
    """Token name of the HTTP caller, or 'local' for stdio/unauthenticated use."""
    token = get_access_token()
    return token.client_id if token is not None else "local"


def get_previews(ctx: Context) -> PreviewStore:
    store: PreviewStore = ctx.lifespan_context["previews"]
    return store
