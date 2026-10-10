"""Device / VM summaries and interface detail."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.config import Settings
from netbox_mcp.deps import get_netbox
from netbox_mcp.formatting import compact, compact_dict
from netbox_mcp.tools.common import choice, iface_path, label, resolve_host, resolve_interface

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

# Large or rarely useful fields left out of summaries (use netbox_get_object for everything).
_HOST_SKIP = {"config_context", "local_context_data", "custom_fields", "comments"}
# Interface fields replaced by friendlier summaries in netbox_interface_detail.
_IFACE_SKIP = {
    "link_peers",
    "connected_endpoints",
    "connected_endpoints_reachable",
    "mac_address",
    "primary_mac_address",
    "mac_addresses",
    "untagged_vlan",
    "tagged_vlans",
    "count_ipaddresses",
    "count_fhrp_groups",
}

HostArg = Annotated[str, Field(description="Device or virtual machine name (or numeric id).")]
KindArg = Annotated[
    Literal["device", "vm"] | None,
    Field(description="Force 'device' or 'vm'. Default: try devices, then VMs."),
]


def _vlan(v: dict[str, Any] | None) -> str | None:
    if not v:
        return None
    return f"{v.get('vid')} {v.get('name')}".strip() if v.get("vid") is not None else label(v)


def _peer(iface: dict[str, Any]) -> str | None:
    """'device interface' of the far end (through patch panels), else the directly cabled peer."""
    peers = iface.get("connected_endpoints") or iface.get("link_peers") or []
    names = []
    for p in peers:
        dev = p.get("device") or p.get("circuit") or {}
        names.append(" ".join(x for x in (label(dev), p.get("name") or label(p)) if x))
    return ", ".join(names) or None


def _interface_row(i: dict[str, Any], ips: list[str]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": i["id"],
        "name": i["name"],
        "type": choice(i.get("type")),
        "enabled": None if i.get("enabled", True) else False,
        "mgmt_only": True if i.get("mgmt_only") else None,
        "description": i.get("description"),
        "mode": choice(i.get("mode")),
        "untagged_vlan": _vlan(i.get("untagged_vlan")),
        "tagged_vlans": [v.get("vid") for v in i.get("tagged_vlans") or []],
        "lag": label(i.get("lag")),
        "vrf": label(i.get("vrf")),
        "mac": label(i.get("primary_mac_address")),
        "ips": ips,
        "cable": label(i.get("cable")),
        "peer": _peer(i),
        "parent": label(i.get("parent")),
    }
    return compact_dict(row)


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def netbox_device_summary(
        ctx: Context,
        name: HostArg,
        kind: KindArg = None,
        include_interfaces: bool = True,
        connected_only: Annotated[
            bool, Field(description="Only list interfaces that are cabled or have IPs.")
        ] = False,
    ) -> dict[str, Any]:
        """One-call overview of a device or virtual machine: site/rack/role/type/platform/status,
        primary IPs, and every interface with its IPs, VLANs, LAG, MAC, cable and far-end peer."""
        nb = get_netbox(ctx)
        hk, host = await resolve_host(nb, name, kind)
        out: dict[str, Any] = {"kind": hk, **compact({k: v for k, v in host.items() if k not in _HOST_SKIP})}
        if out.get("primary_ip") in (out.get("primary_ip4"), out.get("primary_ip6")):
            out.pop("primary_ip", None)  # same object as primary_ip4/6
        if host.get("custom_fields") and (cf := compact(host["custom_fields"])):
            out["custom_fields"] = cf
        if not include_interfaces:
            return out

        host_filter = {"device_id": host["id"]} if hk == "device" else {"virtual_machine_id": host["id"]}
        (ifaces, total), (ips, _) = await asyncio.gather(
            get_netbox(ctx).list(iface_path(hk), host_filter, limit=1000),
            get_netbox(ctx).list("ipam/ip-addresses/", host_filter, limit=1000),
        )
        by_iface: dict[int, list[str]] = defaultdict(list)
        for ip in ips:
            if ip.get("assigned_object_id") is not None:
                by_iface[ip["assigned_object_id"]].append(ip["address"])

        rows = [_interface_row(i, by_iface.get(i["id"], [])) for i in ifaces]
        if connected_only:
            rows = [r for r in rows if r.get("cable") or r.get("ips") or r.get("peer")]
        out["interface_count"] = total
        out["interfaces"] = rows
        if total > len(ifaces):
            out["note"] = f"Showing first {len(ifaces)} of {total} interfaces."
        return out

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_interface_detail(
        ctx: Context,
        device: Annotated[
            str | None, Field(description="Device/VM name (or id) that owns the interface.")
        ] = None,
        interface: Annotated[
            str | None, Field(description="Interface name; abbreviations like Gi1/0/1 work.")
        ] = None,
        interface_id: Annotated[
            int | None, Field(description="Interface id (instead of device + name).")
        ] = None,
        kind: KindArg = None,
    ) -> dict[str, Any]:
        """Full detail for one interface: status, mode and VLANs (with names), IPs, MACs, LAG
        membership/members, cable and both the directly cabled peer and the far-end endpoint."""
        nb = get_netbox(ctx)
        hk, iface = await resolve_interface(
            nb, host=device, interface=interface, interface_id=interface_id, kind=kind
        )
        key = "interface_id" if hk == "device" else "vminterface_id"
        tasks: list[Any] = [nb.list("ipam/ip-addresses/", {key: iface["id"]}, limit=100)]
        is_lag = choice(iface.get("type")) == "lag"
        if is_lag:
            tasks.append(nb.list("dcim/interfaces/", {"lag_id": iface["id"], "brief": "true"}, limit=100))
        results = await asyncio.gather(*tasks)
        ips = results[0][0]

        out: dict[str, Any] = {
            "kind": hk,
            **compact({k: v for k, v in iface.items() if k not in _IFACE_SKIP}),
        }
        # Defaults that only matter when they differ from the norm.
        for flag, default in (("enabled", True), ("mgmt_only", False), ("mark_connected", False)):
            if out.get(flag) == default:
                out.pop(flag)
        primary_mac = label(iface.get("primary_mac_address"))
        out["mac_addresses"] = [
            f"{m_label} (primary)" if m_label == primary_mac else m_label
            for m_label in (label(m) for m in iface.get("mac_addresses") or [])
        ]
        out["untagged_vlan"] = _vlan(iface.get("untagged_vlan"))
        out["tagged_vlans"] = [_vlan(v) for v in iface.get("tagged_vlans") or []]
        out["ip_addresses"] = [
            compact({k: ip.get(k) for k in ("id", "address", "status", "role", "vrf", "dns_name")})
            for ip in ips
        ]
        if is_lag:
            out["lag_members"] = [m.get("name") or label(m) for m in results[1][0]]
        if iface.get("cable") or iface.get("link_peers"):
            out["link_peer"] = (
                ", ".join(
                    " ".join(x for x in (label(p.get("device")), p.get("name") or label(p)) if x)
                    for p in iface.get("link_peers") or []
                )
                or None
            )
            out["far_end"] = _peer(iface)
        return compact_dict(out)
