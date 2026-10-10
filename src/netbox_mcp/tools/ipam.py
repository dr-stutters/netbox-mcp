"""IPAM helpers: prefix utilisation, next-available IPs/prefixes, VLAN lookup."""

from __future__ import annotations

import asyncio
import ipaddress
from collections import defaultdict
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.config import Settings
from netbox_mcp.deps import get_netbox
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.formatting import compact, compact_dict
from netbox_mcp.tools.common import choice, label, resolve_prefix

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

PrefixArg = Annotated[
    str | int, Field(description="Prefix in CIDR form (e.g. 10.0.0.0/24) or its numeric id.")
]
VrfArg = Annotated[
    str | int | None,
    Field(description="VRF name, RD or id, to pick between identical prefixes in different VRFs."),
]


def _vrf_filter(prefix: dict[str, Any]) -> dict[str, Any]:
    vrf = prefix.get("vrf")
    return {"vrf_id": vrf["id"] if vrf else "null"}


def _prefix_brief(p: dict[str, Any]) -> dict[str, Any]:
    return compact_dict(
        {k: p.get(k) for k in ("id", "prefix", "status", "vlan", "role", "tenant", "description")}
    )


def usable_size(net: ipaddress.IPv4Network | ipaddress.IPv6Network, is_pool: bool) -> int:
    """Same rule as NetBox: IPv4 prefixes shorter than /31 lose network+broadcast unless a pool."""
    if net.version == 4 and net.prefixlen < 31 and not is_pool:
        return net.num_addresses - 2
    return net.num_addresses


def covered_addresses(prefixes: list[str]) -> int:
    """Number of addresses covered by a set of (possibly nested/overlapping) prefixes."""
    v4 = [n for n in map(ipaddress.ip_network, prefixes) if isinstance(n, ipaddress.IPv4Network)]
    v6 = [n for n in map(ipaddress.ip_network, prefixes) if isinstance(n, ipaddress.IPv6Network)]
    return sum(n.num_addresses for n in ipaddress.collapse_addresses(v4)) + sum(
        n.num_addresses for n in ipaddress.collapse_addresses(v6)
    )


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def netbox_prefix_utilization(
        ctx: Context, prefix: PrefixArg, vrf: VrfArg = None
    ) -> dict[str, Any]:
        """Utilisation of a prefix, computed like NetBox does: for containers, the share covered by
        child prefixes; otherwise the share of usable addresses recorded as IPs. Also lists the direct
        child prefixes and the first free IP or free blocks."""
        nb = get_netbox(ctx)
        p = await resolve_prefix(nb, prefix, vrf)
        net = ipaddress.ip_network(p["prefix"])
        status = choice(p.get("status"))
        vrf_f = _vrf_filter(p)

        children, child_count = await nb.list("ipam/prefixes/", {"within": str(net), **vrf_f}, limit=1000)
        direct = [c for c in children if c.get("_depth") == p.get("_depth", 0) + 1]

        out: dict[str, Any] = {
            "id": p["id"],
            "prefix": str(net),
            "status": status,
            "vrf": label(p.get("vrf")) or "global",
            "vlan": label(p.get("vlan")),
            "scope": label(p.get("scope")),
            "description": p.get("description") or None,
        }
        if p.get("mark_utilized"):
            out["utilization_pct"] = 100.0
            out["note"] = "Prefix is marked as fully utilised."
        elif status == "container":
            covered = covered_addresses([c["prefix"] for c in children])
            out["size"] = net.num_addresses
            out["used"] = covered
            out["utilization_pct"] = round(100 * covered / net.num_addresses, 2)
            free = await nb.get(f"ipam/prefixes/{p['id']}/available-prefixes/")
            out["free_blocks"] = [f["prefix"] for f in free[:20]]
        else:
            _, ip_count = await nb.list(
                "ipam/ip-addresses/", {"parent": str(net), **vrf_f, "brief": "true"}, limit=1
            )
            size = usable_size(net, bool(p.get("is_pool")))
            out["size"] = size
            out["used"] = ip_count
            out["utilization_pct"] = round(100 * ip_count / size, 2) if size else 100.0
            first_free = await nb.get(f"ipam/prefixes/{p['id']}/available-ips/", {"limit": 1})
            out["first_available_ip"] = first_free[0]["address"] if first_free else None
        out["child_prefix_count"] = child_count
        out["child_prefixes"] = [_prefix_brief(c) for c in direct[:50]]
        return compact_dict(out)

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_next_available_ips(
        ctx: Context,
        prefix: PrefixArg,
        count: Annotated[int, Field(ge=1, le=100)] = 1,
        vrf: VrfArg = None,
    ) -> dict[str, Any]:
        """Peek at the next free IP addresses in a prefix (read-only: nothing is reserved)."""
        nb = get_netbox(ctx)
        p = await resolve_prefix(nb, prefix, vrf)
        free = await nb.get(f"ipam/prefixes/{p['id']}/available-ips/", {"limit": count})
        return {
            "prefix": p["prefix"],
            "prefix_id": p["id"],
            "vrf": label(p.get("vrf")) or "global",
            "available": [f["address"] for f in free[:count]],
        }

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_next_available_prefixes(
        ctx: Context,
        prefix: Annotated[str | int, Field(description="Parent prefix (CIDR or id) to carve from.")],
        prefix_length: Annotated[
            int | None,
            Field(description="Wanted child size, e.g. 24. Omit to list the free blocks.", ge=1, le=128),
        ] = None,
        count: Annotated[int, Field(ge=1, le=100)] = 1,
        vrf: VrfArg = None,
    ) -> dict[str, Any]:
        """Peek at free space inside a parent prefix: either the raw free blocks, or the next N
        child prefixes of a given length (read-only: nothing is reserved)."""
        nb = get_netbox(ctx)
        p = await resolve_prefix(nb, prefix, vrf)
        parent = ipaddress.ip_network(p["prefix"])
        free = [
            ipaddress.ip_network(f["prefix"])
            for f in await nb.get(f"ipam/prefixes/{p['id']}/available-prefixes/")
        ]
        out: dict[str, Any] = {
            "parent": str(parent),
            "parent_id": p["id"],
            "vrf": label(p.get("vrf")) or "global",
        }
        if prefix_length is None:
            out["free_blocks"] = [str(n) for n in free]
            return out
        if prefix_length < parent.prefixlen or prefix_length > parent.max_prefixlen:
            raise InvalidRequest(f"/{prefix_length} cannot fit inside {parent}.")
        found: list[str] = []
        for block in free:
            if block.prefixlen > prefix_length:
                continue
            for sub in block.subnets(new_prefix=prefix_length):
                found.append(str(sub))
                if len(found) >= count:
                    break
            if len(found) >= count:
                break
        out["available"] = found
        if len(found) < count:
            out["note"] = f"Only {len(found)} free /{prefix_length} block(s) available."
        return out

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_vlan_lookup(
        ctx: Context,
        vid: Annotated[int | None, Field(description="VLAN ID (1-4094).", ge=1, le=4094)] = None,
        name: Annotated[str | None, Field(description="VLAN name (case-insensitive, partial match).")] = None,
        site: Annotated[str | None, Field(description="Site slug to narrow the search.")] = None,
        group: Annotated[str | None, Field(description="VLAN group slug to narrow the search.")] = None,
    ) -> dict[str, Any]:
        """Find VLANs by VID and/or name, with their group/site, the prefixes on them, and the
        device/VM interfaces that carry them (tagged or untagged)."""
        if vid is None and not name:
            raise InvalidRequest("Pass vid and/or name.")
        nb = get_netbox(ctx)
        params: dict[str, Any] = {}
        if vid is not None:
            params["vid"] = vid
        if name:
            params["name__ic"] = name
        if site:
            params["site"] = site
        if group:
            params["group"] = group
        vlans, total = await nb.list("ipam/vlans/", params, limit=10)

        async def describe(v: dict[str, Any]) -> dict[str, Any]:
            (prefixes, _), (ifaces, if_count), (vm_ifaces, vm_count) = await asyncio.gather(
                nb.list("ipam/prefixes/", {"vlan_id": v["id"]}, limit=50),
                nb.list("dcim/interfaces/", {"vlan_id": v["id"]}, limit=200),
                nb.list("virtualization/interfaces/", {"vlan_id": v["id"]}, limit=200),
            )
            carried: dict[str, list[str]] = defaultdict(list)
            for i in [*ifaces, *vm_ifaces]:
                owner = label(i.get("device") or i.get("virtual_machine")) or "?"
                untagged = (i.get("untagged_vlan") or {}).get("id") == v["id"]
                carried[owner].append(f"{i['name']} ({'untagged' if untagged else 'tagged'})")
            entry = compact(
                {
                    k: v.get(k)
                    for k in ("id", "vid", "name", "status", "group", "site", "tenant", "role", "description")
                }
            )
            entry["prefixes"] = [
                f"{p['prefix']}" + (f" ({label(p['vrf'])})" if p.get("vrf") else "") for p in prefixes
            ]
            entry["interface_count"] = if_count + vm_count
            entry["interfaces"] = dict(carried)
            return compact_dict(entry)

        out: dict[str, Any] = {
            "count": total,
            "vlans": list(await asyncio.gather(*(describe(v) for v in vlans))),
        }
        if total > len(vlans):
            out["note"] = f"Showing {len(vlans)} of {total}; narrow with site/group/vid."
        return out
