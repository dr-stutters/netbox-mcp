"""Summaries for circuits, VPN tunnels, L2VPNs, wireless LANs and power chains."""

from __future__ import annotations

import asyncio
import math
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.deps import get_netbox
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.formatting import compact, compact_dict
from netbox_mcp.tools.cabling import terms_label
from netbox_mcp.tools.common import choice, label, resolve_host

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

MAX_POWER_HOPS = 6  # outlet -> PDU inlet -> ... chains (PDUs fed by PDUs)


async def _one(
    nb: NetBoxClient,
    path: str,
    filters: list[dict[str, Any]],
    what: str,
    ref: Any,
    *,
    numeric_is_id: bool = True,
) -> dict[str, Any]:
    """First filter set that matches exactly one object; clear errors otherwise. A numeric ref is
    taken as the object id unless numeric_is_id=False (then put an {"id": ...} filter in `filters`)."""
    if numeric_is_id and (isinstance(ref, int) or str(ref).isdigit()):
        return await nb.get_object(f"{path}{int(ref)}/")
    for f in filters:
        found, count = await nb.list(path, f, limit=10)
        if count == 1:
            return found[0]
        if count > 1:
            raise InvalidRequest(
                f"{what} {ref!r} matches {count}: {', '.join(label(o) or '?' for o in found)}."
            )
    raise InvalidRequest(f"No {what} matching {ref!r}.")


def _owner_and_name(obj: dict[str, Any] | None) -> str | None:
    """'device interface' for a component, or the object's label."""
    if not obj:
        return None
    owner = obj.get("device") or obj.get("virtual_machine")
    name = obj.get("name") or label(obj)
    return f"{label(owner)} {name}" if owner else name


def _watts(feed: dict[str, Any]) -> int:
    """Usable power of a feed, as NetBox computes it (three-phase multiplies by sqrt(3))."""
    power = (
        (feed.get("voltage") or 0) * (feed.get("amperage") or 0) * (feed.get("max_utilization") or 0) / 100
    )
    if choice(feed.get("phase")) == "three-phase":
        power *= math.sqrt(3)
    return round(power)


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def netbox_circuit_summary(
        ctx: Context, circuit: Annotated[str | int, Field(description="Circuit ID (cid) or numeric id.")]
    ) -> dict[str, Any]:
        """A circuit end to end: provider and account, type, status, commit rate and dates, and for each
        side (A/Z) where it terminates (site / provider network), port speed, cross-connect and the
        device + interface cabled to it."""
        nb = get_netbox(ctx)
        c = await _one(nb, "circuits/circuits/", [{"cid": circuit}, {"cid__ic": circuit}], "circuit", circuit)
        terms, _ = await nb.list("circuits/circuit-terminations/", {"circuit_id": c["id"]}, limit=10)
        sides = {}
        for t in sorted(terms, key=lambda t: t.get("term_side") or ""):
            sides[t.get("term_side") or "?"] = compact_dict(
                {
                    "terminates_at": label(t.get("termination")),
                    "termination_type": t.get("termination_type"),
                    "port_speed_kbps": t.get("port_speed"),
                    "upstream_speed_kbps": t.get("upstream_speed"),
                    "xconnect_id": t.get("xconnect_id"),
                    "patch_panel": t.get("pp_info"),
                    "cable": label(t.get("cable")),
                    "connected_to": terms_label(t.get("link_peers")),
                    "marked_connected": t.get("mark_connected") or None,
                }
            )
        return compact_dict(
            {
                "cid": c.get("cid"),
                "provider": label(c.get("provider")),
                "provider_account": label(c.get("provider_account")),
                "type": label(c.get("type")),
                "status": choice(c.get("status")),
                "tenant": label(c.get("tenant")),
                "commit_rate_kbps": c.get("commit_rate"),
                "install_date": c.get("install_date"),
                "termination_date": c.get("termination_date"),
                "description": c.get("description"),
                "sides": sides,
            }
        )

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_tunnel_summary(
        ctx: Context,
        tunnel: Annotated[
            str | int | None, Field(description="Tunnel name or id. Omit to list all tunnels.")
        ] = None,
    ) -> dict[str, Any]:
        """A VPN tunnel: encapsulation, status, each endpoint (device/VM + interface, role, outside IP)
        and the IPsec profile with its IKE/IPsec policies and proposals. Pre-shared keys are never
        returned. Without a name, lists all tunnels."""
        nb = get_netbox(ctx)
        if tunnel is None:
            tunnels, total = await nb.list("vpn/tunnels/", {}, limit=200)
            return {
                "count": total,
                "tunnels": [
                    compact(
                        {
                            "name": t.get("name"),
                            "status": choice(t.get("status")),
                            "encapsulation": choice(t.get("encapsulation")),
                            "group": label(t.get("group")),
                            "endpoints": t.get("terminations_count"),
                        }
                    )
                    for t in tunnels
                ],
            }
        t = await _one(nb, "vpn/tunnels/", [{"name": tunnel}, {"name__ic": tunnel}], "tunnel", tunnel)
        terms, _ = await nb.list("vpn/tunnel-terminations/", {"tunnel_id": t["id"]}, limit=100)
        out: dict[str, Any] = {
            "name": t.get("name"),
            "status": choice(t.get("status")),
            "encapsulation": choice(t.get("encapsulation")),
            "group": label(t.get("group")),
            "tunnel_id": t.get("tunnel_id"),
            "tenant": label(t.get("tenant")),
            "description": t.get("description"),
            "endpoints": [
                compact(
                    {
                        "role": choice(x.get("role")),
                        "interface": _owner_and_name(x.get("termination")),
                        "outside_ip": label(x.get("outside_ip")),
                    }
                )
                for x in terms
            ],
        }
        if t.get("ipsec_profile"):
            out["ipsec"] = await _ipsec_profile(nb, t["ipsec_profile"]["id"])
        return compact_dict(out)

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_l2vpn_summary(
        ctx: Context,
        l2vpn: Annotated[
            str | int | None,
            Field(description="L2VPN name, slug or identifier (e.g. VNI). Omit to list all."),
        ] = None,
    ) -> dict[str, Any]:
        """An L2VPN (VXLAN/EVPN/VPLS/...): type, identifier, route targets, and its members: VLANs,
        device interfaces and VM interfaces. Without a name, lists all L2VPNs."""
        nb = get_netbox(ctx)
        if l2vpn is None:
            items, total = await nb.list("vpn/l2vpns/", {}, limit=200)
            return {
                "count": total,
                "l2vpns": [
                    compact(
                        {
                            "name": v.get("name"),
                            "type": choice(v.get("type")),
                            "identifier": v.get("identifier"),
                        }
                    )
                    for v in items
                ],
            }
        filters: list[dict[str, Any]] = [{"name": l2vpn}, {"slug": l2vpn}]
        if str(l2vpn).isdigit():
            # A number is most likely the identifier (VNI / VC ID); fall back to the object id.
            filters = [{"identifier": int(l2vpn)}, *filters, {"id": int(l2vpn)}]
        v = await _one(nb, "vpn/l2vpns/", filters, "L2VPN", l2vpn, numeric_is_id=False)
        terms, _ = await nb.list("vpn/l2vpn-terminations/", {"l2vpn_id": v["id"]}, limit=1000)
        members: dict[str, list[str]] = {"vlans": [], "interfaces": [], "vm_interfaces": []}
        for x in terms:
            kind = x.get("assigned_object_type")
            obj = x.get("assigned_object")
            if kind == "ipam.vlan":
                members["vlans"].append(label(obj) or "?")
            elif kind == "virtualization.vminterface":
                members["vm_interfaces"].append(_owner_and_name(obj) or "?")
            else:
                members["interfaces"].append(_owner_and_name(obj) or "?")
        return compact_dict(
            {
                "name": v.get("name"),
                "type": choice(v.get("type")),
                "identifier": v.get("identifier"),
                "status": choice(v.get("status")),
                "tenant": label(v.get("tenant")),
                "import_targets": [label(rt) for rt in v.get("import_targets") or []],
                "export_targets": [label(rt) for rt in v.get("export_targets") or []],
                "description": v.get("description"),
                "member_count": len(terms),
                **members,
            }
        )

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_wireless_lookup(
        ctx: Context,
        ssid: Annotated[str | None, Field(description="SSID (partial match). Omit to list all.")] = None,
        group: Annotated[
            str | None, Field(description="Wireless LAN group slug to narrow the search.")
        ] = None,
    ) -> dict[str, Any]:
        """Wireless LANs: SSID, group, status, VLAN, security (auth type and cipher; the PSK itself is
        never returned) and the access points / interfaces serving each one, plus wireless links."""
        nb = get_netbox(ctx)
        params: dict[str, Any] = {}
        if ssid:
            params["ssid__ic"] = ssid
        if group:
            params["group"] = group
        wlans, total = await nb.list("wireless/wireless-lans/", params, limit=50)

        async def describe(w: dict[str, Any]) -> dict[str, Any]:
            (ifaces, _), (links, _) = await asyncio.gather(
                nb.list("dcim/interfaces/", {"wireless_lan_id": w["id"]}, limit=200),
                nb.list("wireless/wireless-links/", {"ssid": w.get("ssid")}, limit=50),
            )
            return compact_dict(
                {
                    "ssid": w.get("ssid"),
                    "group": label(w.get("group")),
                    "status": choice(w.get("status")),
                    "vlan": label(w.get("vlan")),
                    "scope": label(w.get("scope")),
                    "auth_type": choice(w.get("auth_type")),
                    "auth_cipher": choice(w.get("auth_cipher")),
                    "psk_set": bool(w.get("auth_psk")) or None,
                    "tenant": label(w.get("tenant")),
                    "served_by": [
                        f"{_owner_and_name(i)} ({choice(i.get('rf_role')) or 'interface'})" for i in ifaces
                    ],
                    "links": [
                        " <-> ".join(
                            _owner_and_name(link.get(side)) or "?" for side in ("interface_a", "interface_b")
                        )
                        for link in links
                    ],
                }
            )

        out: dict[str, Any] = {
            "count": total,
            "wireless_lans": list(await asyncio.gather(*(describe(w) for w in wlans))),
        }
        if not wlans:
            out["note"] = "No wireless LANs match."
        return out

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_power_trace(
        ctx: Context,
        device: Annotated[str | int, Field(description="Device name or id.")],
        port: Annotated[
            str | None, Field(description="One power port (e.g. PSU1). Default: all of them.")
        ] = None,
    ) -> dict[str, Any]:
        """Where a device gets its power: for each power port (PSU), the chain PSU -> PDU outlet ->
        PDU inlet -> ... -> power feed -> power panel, with draw and feed capacity. If the device is in
        a rack, also the rack's power budget (feed capacity vs allocated and maximum draw)."""
        nb = get_netbox(ctx)
        _, dev = await resolve_host(nb, device, "device")
        params: dict[str, Any] = {"device_id": dev["id"]}
        if port:
            params["name"] = port
        ports, _ = await nb.list("dcim/power-ports/", params, limit=50)
        if not ports:
            raise InvalidRequest(f"{label(dev)} has no power ports{' named ' + repr(port) if port else ''}.")
        chains = await asyncio.gather(*(_power_chain(nb, p) for p in ports))
        out: dict[str, Any] = {"device": label(dev), "power_ports": list(chains)}
        if dev.get("rack"):
            out["rack_budget"] = await _rack_budget(nb, dev["rack"])
        return compact_dict(out)


async def _nothing() -> dict[str, Any]:
    return {}


async def _ipsec_profile(nb: NetBoxClient, profile_id: int) -> dict[str, Any]:
    profile = await nb.get_object(f"vpn/ipsec-profiles/{profile_id}/")
    ike_id = (profile.get("ike_policy") or {}).get("id")
    esp_id = (profile.get("ipsec_policy") or {}).get("id")
    ike, esp = await asyncio.gather(
        nb.get_object(f"vpn/ike-policies/{ike_id}/") if ike_id else _nothing(),
        nb.get_object(f"vpn/ipsec-policies/{esp_id}/") if esp_id else _nothing(),
    )

    async def proposals(path: str, refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not refs:
            return []
        items, _ = await nb.list(path, {"id": [r["id"] for r in refs]}, limit=len(refs))
        return [
            compact(
                {
                    "name": p.get("name"),
                    "auth_method": choice(p.get("authentication_method")),
                    "encryption": choice(p.get("encryption_algorithm")),
                    "integrity": choice(p.get("authentication_algorithm")),
                    "dh_group": choice(p.get("group")),
                    "lifetime_s": p.get("sa_lifetime") or p.get("sa_lifetime_seconds"),
                }
            )
            for p in items
        ]

    ike_props, esp_props = await asyncio.gather(
        proposals("vpn/ike-proposals/", ike.get("proposals") or []),
        proposals("vpn/ipsec-proposals/", esp.get("proposals") or []),
    )
    return compact_dict(
        {
            "profile": profile.get("name"),
            "mode": choice(profile.get("mode")),
            "ike_policy": compact(
                {
                    "name": ike.get("name"),
                    "version": choice(ike.get("version")),
                    "mode": choice(ike.get("mode")),
                    "preshared_key_set": bool(ike.get("preshared_key")) or None,
                    "proposals": ike_props,
                }
            ),
            "ipsec_policy": compact(
                {"name": esp.get("name"), "pfs_group": choice(esp.get("pfs_group")), "proposals": esp_props}
            ),
        }
    )


async def _power_chain(nb: NetBoxClient, port: dict[str, Any]) -> dict[str, Any]:
    """Follow a power port upstream: cable to an outlet, the outlet's own inlet, and so on to a feed."""
    hops: list[str] = []
    current: dict[str, Any] | None = port
    feed: dict[str, Any] | None = None
    for _ in range(MAX_POWER_HOPS):
        if current is None:
            break
        hops.append(_power_port_label(current))
        segments = await nb.get(f"dcim/power-ports/{current['id']}/trace/") or []
        far = segments[-1][2] if segments and segments[-1][2] else []
        if not far:
            break
        end = far[0]
        if "power_panel" in end or "/power-feeds/" in (end.get("url") or ""):
            feed = await nb.get_object(f"dcim/power-feeds/{end['id']}/")
            break
        outlet = await nb.get_object(f"dcim/power-outlets/{end['id']}/")
        leg = f", leg {choice(outlet['feed_leg'])}" if outlet.get("feed_leg") else ""
        hops.append(f"{_owner_and_name(outlet)} (outlet{leg})")
        inlet = outlet.get("power_port")
        current = await nb.get_object(f"dcim/power-ports/{inlet['id']}/") if inlet else None
    chain: dict[str, Any] = {
        "port": port.get("name"),
        "maximum_draw_w": port.get("maximum_draw"),
        "allocated_draw_w": port.get("allocated_draw"),
        "chain": hops,
    }
    if feed:
        chain["chain"].append(f"{feed.get('name')} (feed, {feed.get('voltage')}V {feed.get('amperage')}A)")
        chain["chain"].append(f"{label(feed.get('power_panel'))} (panel)")
        chain["feed"] = compact(
            {
                "name": feed.get("name"),
                "status": choice(feed.get("status")),
                "type": choice(feed.get("type")),
                "supply": choice(feed.get("supply")),
                "phase": choice(feed.get("phase")),
                "available_w": _watts(feed),
            }
        )
    else:
        chain["note"] = "Chain does not reach a power feed (unconnected cable or outlet without an inlet)."
    return compact_dict(chain)


def _power_port_label(p: dict[str, Any]) -> str:
    draw = ", ".join(
        f"{k} {p[f]} W"
        for k, f in (("allocated", "allocated_draw"), ("max", "maximum_draw"))
        if p.get(f) is not None
    )
    return f"{_owner_and_name(p)} (power port{', ' + draw if draw else ''})"


async def _rack_budget(nb: NetBoxClient, rack: dict[str, Any]) -> dict[str, Any]:
    (feeds, _), (ports, _) = await asyncio.gather(
        nb.list("dcim/power-feeds/", {"rack_id": rack["id"]}, limit=100),
        nb.list("dcim/power-ports/", {"rack_id": rack["id"]}, limit=2000),
    )
    # Ports fed directly by a feed are PDU inlets: their load is the sum of what's plugged into the PDU.
    load_ports = [p for p in ports if p.get("connected_endpoints_type") != "dcim.powerfeed"]
    capacity = sum(_watts(f) for f in feeds if choice(f.get("type")) != "redundant")
    allocated = sum(p.get("allocated_draw") or 0 for p in load_ports)
    maximum = sum(p.get("maximum_draw") or 0 for p in load_ports)
    return compact_dict(
        {
            "rack": label(rack),
            "feeds": [f"{f.get('name')} ({choice(f.get('type'))}, {_watts(f)} W)" for f in feeds],
            "capacity_w": capacity,
            "allocated_draw_w": allocated,
            "maximum_draw_w": maximum,
            "utilization_pct": round(100 * allocated / capacity, 1) if capacity else None,
        }
    )
