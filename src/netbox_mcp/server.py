"""FastMCP server construction."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from fastmcp.server.lifespan import lifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from starlette.requests import Request
from starlette.responses import JSONResponse

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.deps import current_caller
from netbox_mcp.errors import NetBoxError
from netbox_mcp.preview import PreviewStore
from netbox_mcp.schema import SchemaStore
from netbox_mcp.tools import (
    analysis,
    automation,
    cabling,
    config,
    devices,
    domains,
    generic,
    history,
    hygiene,
    ipam,
    search,
    sites,
    spec,
    system,
    tenancy,
    writes,
)

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Tools for NetBox, the network source of truth (DCIM + IPAM): sites, racks, devices, interfaces,
cables, IP addresses, prefixes, VLANs, VRFs and virtual machines.
- Start with the task tools: netbox_search, netbox_lookup_ip, netbox_lookup_mac,
  netbox_device_summary, netbox_interface_detail, netbox_whats_connected, netbox_trace_cable,
  netbox_site_summary, netbox_rack_elevation, netbox_find_rack_space, netbox_prefix_utilization,
  netbox_next_available_ips, netbox_next_available_prefixes, netbox_next_available_vlans,
  netbox_next_available_asns, netbox_vlan_lookup,
  netbox_change_history, netbox_journal, netbox_data_hygiene, netbox_render_config,
  netbox_config_context, netbox_custom_fields, netbox_circuit_summary, netbox_tunnel_summary,
  netbox_l2vpn_summary, netbox_wireless_lookup, netbox_power_trace, netbox_cluster_summary,
  netbox_service_lookup, netbox_tenant_summary, netbox_contacts_for, netbox_topology,
  netbox_impact_analysis, netbox_compare, netbox_list_scripts, netbox_job_status,
  netbox_export, netbox_graphql.
- netbox_list_objects / netbox_get_object work for any object type ('dcim.devices', 'ipam.prefixes', ...).
- Not sure of a type, field or filter name? netbox_get_definition and netbox_search_spec describe the API.
- netbox_api_call reaches any endpoint the other tools don't cover.
Call netbox_status if anything fails; it reports the NetBox version, the API user,
and whether write tools are enabled on this server.
"""

WRITE_INSTRUCTIONS = """
Writes are ENABLED: netbox_create_object, netbox_update_object, netbox_delete_object,
netbox_allocate_ips, netbox_allocate_prefix, netbox_allocate_vlans, netbox_allocate_asns,
netbox_connect_cable, netbox_add_journal_entry, netbox_run_script (commit=true), and non-GET
netbox_api_call change live data.
All of them are PREVIEW-FIRST: a call without `confirm` changes nothing and returns a preview
(field changes, side effects, exact addresses/VLANs/ASNs) plus a single-use confirm token. Show the
preview to the user and only after they approve repeat the identical call with confirm=<token>.
Tokens expire after 10 minutes and are refused if the arguments or the objects changed; then preview
again. A "blocked" preview lists why the change can't be applied. Prefer the allocate tools over
picking addresses yourself.
"""


class CallLogger(Middleware):
    """Log every tool call with the caller's token name, so multi-user servers have an audit trail."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        log.info("tool=%s caller=%s", context.message.name, current_caller())
        return await call_next(context)


def create_server(settings: Settings, *, auth: AuthProvider | None = None) -> FastMCP:
    """Build the server. `auth` protects the HTTP transport (stdio is always local and unauthenticated)."""

    @lifespan
    async def netbox_lifespan(server: FastMCP[Any]) -> AsyncIterator[dict[str, Any]]:
        client = NetBoxClient(
            settings.netbox_url,
            settings.netbox_token.get_secret_value(),
            verify=settings.verify,
            timeout=settings.netbox_timeout,
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
            yield {
                "netbox": client,
                "schema": SchemaStore(client, settings.netbox_mcp_cache_dir),
                "previews": PreviewStore(),
            }
        finally:
            await client.aclose()

    instructions = INSTRUCTIONS + (WRITE_INSTRUCTIONS if settings.netbox_mcp_enable_writes else "")
    mcp: FastMCP = FastMCP(
        name="netbox",
        instructions=instructions,
        lifespan=netbox_lifespan,
        mask_error_details=False,
        auth=auth,
        middleware=[CallLogger()],
    )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        """Liveness check for Docker/load balancers. Unauthenticated, so it reveals nothing about NetBox."""
        return JSONResponse({"status": "ok"})

    for module in (
        system,
        search,
        devices,
        cabling,
        ipam,
        sites,
        domains,
        history,
        hygiene,
        config,
        tenancy,
        analysis,
        automation,
        generic,
        spec,
        writes,
    ):
        module.register(mcp, settings)
    return mcp
