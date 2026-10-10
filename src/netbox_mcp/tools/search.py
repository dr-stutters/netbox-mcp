"""Global search and IP/MAC lookups."""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.deps import get_netbox, get_schema
from netbox_mcp.errors import NetBoxError
from netbox_mcp.formatting import compact, compact_dict
from netbox_mcp.tools.common import label, normalise_mac, parse_ip, resolve_vrf

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

DEFAULT_SEARCH_TYPES = [
    "dcim.sites",
    "dcim.devices",
    "virtualization.virtual-machines",
    "dcim.interfaces",
    "ipam.ip-addresses",
    "ipam.prefixes",
    "ipam.vlans",
    "ipam.vrfs",
    "dcim.racks",
    "dcim.cables",
    "circuits.circuits",
    "tenancy.tenants",
]


def _summarise_ip(ip: dict[str, Any]) -> dict[str, Any]:
    out = compact_dict(
        {
            k: ip.get(k)
            for k in (
                "id",
                "address",
                "status",
                "role",
                "vrf",
                "tenant",
                "dns_name",
                "description",
                "nat_inside",
                "nat_outside",
            )
        }
    )
    obj = ip.get("assigned_object")
    if obj:
        out["assigned_to"] = {
            "type": ip.get("assigned_object_type"),
            "interface": obj.get("name") or obj.get("display"),
            "interface_id": obj.get("id"),
            **({"device": label(obj["device"])} if obj.get("device") else {}),
            **({"virtual_machine": label(obj["virtual_machine"])} if obj.get("virtual_machine") else {}),
        }
    return out


async def _count_and_items(
    nb: NetBoxClient, path: str, params: dict[str, Any], limit: int
) -> tuple[int, list[Any]]:
    try:
        items, count = await nb.list(path, params, limit=limit)
    except NetBoxError:
        return 0, []  # e.g. the token can't view this type; don't fail the whole search
    return count, items


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def netbox_search(
        ctx: Context,
        query: Annotated[
            str, Field(description="Text to search for: a name, IP, prefix, serial, cable label, ...")
        ],
        object_types: Annotated[
            list[str] | None,
            Field(
                description="Limit to these types (e.g. ['dcim.devices']). Default: common DCIM/IPAM types."
            ),
        ] = None,
        limit_per_type: Annotated[int, Field(ge=1, le=50)] = 5,
    ) -> dict[str, Any]:
        """Search NetBox across many object types at once (devices, VMs, interfaces, IPs, prefixes,
        VLANs, sites, racks, cables, circuits, ...). Returns matches grouped by type with total counts.
        For an IP or MAC, netbox_lookup_ip / netbox_lookup_mac give much richer context."""
        nb = get_netbox(ctx)
        if object_types:
            schema = await get_schema(ctx)
            types = [schema.resolve_type(t).key for t in object_types]
        else:
            types = DEFAULT_SEARCH_TYPES
        paths = {t: t.replace(".", "/") + "/" for t in types}
        found = await asyncio.gather(
            *(_count_and_items(nb, paths[t], {"q": query, "brief": "true"}, limit_per_type) for t in types)
        )
        results: dict[str, Any] = {}
        for t, (count, items) in zip(types, found, strict=True):
            if count:
                results[t] = {"count": count, "results": [compact(i) for i in items]}
        out: dict[str, Any] = {"query": query, "results": results}
        if not results:
            out["note"] = "No matches. Try a shorter/partial term, or netbox_list_objects with filters."
        try:
            ipaddress.ip_interface(query.strip())
            out["hint"] = "Query looks like an IP: netbox_lookup_ip gives the owning device, prefix and VLAN."
        except ValueError:
            pass
        return out

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_lookup_ip(
        ctx: Context,
        address: Annotated[
            str, Field(description="IP address, with or without mask (e.g. 10.1.2.3 or 10.1.2.3/24).")
        ],
        vrf: Annotated[
            str | int | None, Field(description="VRF name, RD or id. Default: search all VRFs.")
        ] = None,
    ) -> dict[str, Any]:
        """Everything NetBox knows about an IP: the IP address object(s) and what they're assigned to
        (device/VM + interface), the containing prefixes (most specific last) with VLAN/site/VRF,
        and any IP ranges containing it. Works even if the IP itself isn't recorded."""
        nb = get_netbox(ctx)
        host = str(parse_ip(address).ip)
        vrf_id = await resolve_vrf(nb, vrf)
        ip_params: dict[str, Any] = {"address": host}
        net_params: dict[str, Any] = {"contains": host}
        if vrf_id is not None:
            ip_params["vrf_id"] = vrf_id
            net_params["present_in_vrf_id"] = vrf_id
        (ips, _), (prefixes, _), (ranges, _) = await asyncio.gather(
            nb.list("ipam/ip-addresses/", ip_params, limit=20),
            nb.list("ipam/prefixes/", net_params, limit=50),
            nb.list(
                "ipam/ip-ranges/", {"contains": host, **({"vrf_id": vrf_id} if vrf_id else {})}, limit=10
            ),
        )
        prefixes.sort(key=lambda p: (p.get("_depth", 0), ipaddress.ip_network(p["prefix"]).prefixlen))
        out: dict[str, Any] = {
            "address": host,
            "ip_addresses": [_summarise_ip(ip) for ip in ips],
            "prefixes": [
                compact(
                    {
                        k: p.get(k)
                        for k in (
                            "id",
                            "prefix",
                            "status",
                            "vrf",
                            "vlan",
                            "scope",
                            "role",
                            "tenant",
                            "description",
                        )
                    }
                )
                for p in prefixes
            ],
        }
        if ranges:
            out["ip_ranges"] = [
                compact(
                    {
                        k: r.get(k)
                        for k in ("id", "start_address", "end_address", "status", "vrf", "description")
                    }
                )
                for r in ranges
            ]
        if not ips:
            out["note"] = "This address is not recorded as an IP address object in NetBox."
        return out

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_lookup_mac(
        ctx: Context,
        mac: Annotated[
            str, Field(description="MAC address in any common format (aa:bb:.., aa-bb-.., aabb.ccdd.eeff).")
        ],
    ) -> dict[str, Any]:
        """Find which device/VM interface a MAC address belongs to, plus the IPs on that interface."""
        nb = get_netbox(ctx)
        normalised = normalise_mac(mac)
        macs, _ = await nb.list("dcim/mac-addresses/", {"mac_address": normalised}, limit=20)

        async def describe(m: dict[str, Any]) -> dict[str, Any]:
            entry: dict[str, Any] = compact_dict({k: m.get(k) for k in ("id", "mac_address", "description")})
            obj = m.get("assigned_object")
            if not obj:
                entry["assigned_to"] = None
                return entry
            otype = m.get("assigned_object_type")
            entry["assigned_to"] = {
                "type": otype,
                "interface": obj.get("name") or obj.get("display"),
                "interface_id": obj["id"],
                **({"device": label(obj["device"])} if obj.get("device") else {}),
                **({"virtual_machine": label(obj["virtual_machine"])} if obj.get("virtual_machine") else {}),
            }
            key = "interface_id" if otype == "dcim.interface" else "vminterface_id"
            ips, _ = await nb.list("ipam/ip-addresses/", {key: obj["id"]}, limit=20)
            entry["ip_addresses"] = [
                ip["address"] + (f" ({label(ip['vrf'])})" if ip.get("vrf") else "") for ip in ips
            ]
            return compact_dict(entry)

        results = await asyncio.gather(*(describe(m) for m in macs))
        out: dict[str, Any] = {"mac": normalised, "matches": list(results)}
        if not macs:
            out["note"] = "MAC address not recorded in NetBox."
        return out
