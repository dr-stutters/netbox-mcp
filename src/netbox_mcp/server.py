"""FastMCP server construction."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.errors import NetBoxError
from netbox_mcp.schema import SchemaStore
from netbox_mcp.tools import cabling, devices, generic, ipam, search, spec, system

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Tools for NetBox, the network source of truth (DCIM + IPAM): sites, racks, devices, interfaces,
cables, IP addresses, prefixes, VLANs, VRFs and virtual machines.
- Start with the task tools: netbox_search, netbox_lookup_ip, netbox_lookup_mac, netbox_device_summary,
  netbox_interface_detail, netbox_trace_cable, netbox_prefix_utilization, netbox_next_available_ips,
  netbox_next_available_prefixes, netbox_vlan_lookup.
- netbox_list_objects / netbox_get_object work for any object type ('dcim.devices', 'ipam.prefixes', ...).
- Not sure of a type, field or filter name? netbox_get_definition and netbox_search_spec describe the API.
- netbox_api_call reaches any endpoint the other tools don't cover.
Call netbox_status if anything fails; it reports the NetBox version, the API user,
and whether write tools are enabled on this server.
"""


def create_server(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> FastMCP:
    """Build the server. `transport` lets tests inject a mocked httpx transport."""

    @lifespan
    async def netbox_lifespan(server: FastMCP[Any]) -> AsyncIterator[dict[str, Any]]:
        client = NetBoxClient(
            settings.netbox_url,
            settings.netbox_token.get_secret_value(),
            verify=settings.verify,
            timeout=settings.netbox_timeout,
            transport=transport,
        )
        if settings.netbox_url.startswith("http://"):
            log.warning("NETBOX_URL uses plain HTTP; the API token is sent unencrypted")
        try:
            status = await client.status()
            log.info("Connected to NetBox %s at %s", status.get("netbox-version"), settings.netbox_url)
        except NetBoxError as e:
            # Don't refuse to start: the model can still call netbox_status to see the error.
            log.error("NetBox startup check failed: %s", e)
        try:
            yield {"netbox": client, "schema": SchemaStore(client, settings.netbox_mcp_cache_dir)}
        finally:
            await client.aclose()

    mcp: FastMCP = FastMCP(
        name="netbox",
        instructions=INSTRUCTIONS,
        lifespan=netbox_lifespan,
        mask_error_details=False,
    )

    for module in (system, search, devices, cabling, ipam, generic, spec):
        module.register(mcp, settings)
    return mcp
