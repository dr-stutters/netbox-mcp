"""Composite analysis: topology graphs, impact analysis and object comparison."""

from __future__ import annotations

import asyncio
import ipaddress
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.deps import get_netbox, get_schema
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.formatting import compact, compact_dict
from netbox_mcp.tools.common import choice, label, resolve_host, resolve_object

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

MAX_NODES = 100
PARALLEL = 8
_IFACE_FIELDS = ",".join(
    ("id", "name", "device", "type", "enabled", "cable", "link_peers", "link_peers_type")
    + ("connected_endpoints", "connected_endpoints_type")
)
_ABBREVIATIONS = (
    ("TwentyFiveGigE", "Twe"),
    ("HundredGigE", "Hu"),
    ("FortyGigabitEthernet", "Fo"),
    ("TenGigabitEthernet", "Te"),
    ("GigabitEthernet", "Gi"),
    ("FastEthernet", "Fa"),
    ("Port-channel", "Po"),
    ("Ethernet", "Eth"),
)
FHRP_ROLES = {"vip", "vrrp", "hsrp", "glbp", "carp", "anycast"}


def short_if(name: str) -> str:
    for long, abbr in _ABBREVIATIONS:
        if name.startswith(long):
            return abbr + name[len(long) :]
    return name


@dataclass(frozen=True)
class Link:
    local_if: str
    local_if_id: int
    peer_kind: str  # "device", or the endpoint type (e.g. circuits.providernetwork)
    peer_id: int | None  # device id for device peers
    peer_name: str
    peer_if: str | None
    via: str | None  # what the cable physically connects to, when that isn't the far end


async def device_links(nb: NetBoxClient, device_id: int) -> list[Link]:
    """A device's connections, from each interface's resolved far end (through patch panels)."""
    ifaces, _ = await nb.list(
        "dcim/interfaces/", {"device_id": device_id, "fields": _IFACE_FIELDS}, limit=2000
    )
    links = []
    for i in ifaces:
        ends = i.get("connected_endpoints") or []
        etype = i.get("connected_endpoints_type") or ""
        direct = i.get("link_peers") or []
        via = None
        if direct and i.get("link_peers_type") != etype:
            p = direct[0]
            if p.get("circuit"):
                via = label(p)  # circuit terminations display as "<cid>: Termination A"
            else:
                via = " ".join(x for x in (label(p.get("device")), p.get("name") or label(p)) if x)
        for e in ends:
            if etype == "dcim.interface" and e.get("device"):
                links.append(
                    Link(
                        i["name"],
                        i["id"],
                        "device",
                        e["device"]["id"],
                        label(e["device"]) or "?",
                        e.get("name"),
                        via,
                    )
                )
            else:
                links.append(Link(i["name"], i["id"], etype, None, label(e) or "?", None, via))
    return links


def _node_id(name: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", name)


def _mermaid_label(text: str) -> str:
    return text.replace('"', "'")


def mermaid(nodes: dict[str, str], edges: list[tuple[str, str, str]], external: set[str]) -> str:
    """nodes: name -> subtitle; edges: (a, b, label); external nodes drawn as stadiums."""
    lines = ["graph LR"]
    for name, subtitle in sorted(nodes.items()):
        text = _mermaid_label(f"{name}<br/>{subtitle}" if subtitle else name)
        shape = f'(["{text}"])' if name in external else f'["{text}"]'
        lines.append(f"  {_node_id(name)}{shape}")
    for a, b, lbl in edges:
        lines.append(f'  {_node_id(a)} ---|"{_mermaid_label(lbl)}"| {_node_id(b)}')
    return "\n".join(lines)


def _flatten(v: Any) -> Any:
    """Make compacted values comparable and readable: references -> their display label."""
    if isinstance(v, dict):
        if "display" in v and "id" in v:
            return v["display"]
        return {k: _flatten(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_flatten(x) for x in v]
    return v


_COMPARE_SKIP = {"id", "name", "display", "created", "last_updated", "description", "comments", "slug"}
_IFACE_COMPARE = (
    "type",
    "enabled",
    "mode",
    "untagged_vlan",
    "tagged_vlans",
    "mtu",
    "lag",
    "mgmt_only",
    "vrf",
)


def compare_dicts(a: dict[str, Any], b: dict[str, Any], skip: set[str]) -> tuple[dict[str, Any], int]:
    keys = sorted((set(a) | set(b)) - skip)
    differences = {k: {"a": _shown(a.get(k)), "b": _shown(b.get(k))} for k in keys if a.get(k) != b.get(k)}
    return differences, len(keys) - len(differences)


def _shown(v: Any) -> Any:
    """Missing/empty values are dropped from compact output, so show them explicitly in a diff."""
    return "(none)" if v in (None, "", [], {}) else v


def _iface_profile(i: dict[str, Any]) -> dict[str, Any]:
    untagged = i.get("untagged_vlan")
    return {
        "type": choice(i.get("type")),
        "enabled": i.get("enabled"),
        "mode": choice(i.get("mode")),
        "untagged_vlan": untagged.get("vid") if untagged else None,
        "tagged_vlans": sorted(v.get("vid") for v in i.get("tagged_vlans") or []),
        "mtu": i.get("mtu"),
        "lag": label(i.get("lag")),
        "mgmt_only": i.get("mgmt_only"),
        "vrf": label(i.get("vrf")),
    }


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def netbox_topology(
        ctx: Context,
        device: Annotated[str | int | None, Field(description="Start from this device (name or id).")] = None,
        site: Annotated[
            str | int | None, Field(description="Or: every device at this site (name/slug/id).")
        ] = None,
        hops: Annotated[int, Field(description="How far to expand from the device.", ge=1, le=4)] = 1,
    ) -> dict[str, Any]:
        """Logical topology (which device connects to which, through any patch panels) around a device
        (N hops) or across a site. Returns a Mermaid diagram plus nodes and an edge list. Circuits and
        provider networks appear as rounded external nodes."""
        if (device is None) == (site is None):
            raise InvalidRequest("Pass exactly one of device or site.")
        nb = get_netbox(ctx)
        limiter = asyncio.Semaphore(PARALLEL)

        async def links_of(dev_id: int) -> tuple[int, list[Link]]:
            async with limiter:
                return dev_id, await device_links(nb, dev_id)

        all_links: dict[int, list[Link]] = {}
        names: dict[int, str] = {}
        scope_ids: set[int] = set()
        if device is not None:
            _, start = await resolve_host(nb, device, "device")
            names[start["id"]] = label(start) or "?"
            frontier = {start["id"]}
            for _ in range(hops):
                results = await asyncio.gather(*(links_of(d) for d in frontier if d not in all_links))
                nxt: set[int] = set()
                for dev_id, links in results:
                    all_links[dev_id] = links
                    for link in links:
                        if link.peer_id is not None:
                            names.setdefault(link.peer_id, link.peer_name)
                            if link.peer_id not in all_links:
                                nxt.add(link.peer_id)
                frontier = nxt
                if not frontier or len(names) >= MAX_NODES:
                    break
            scope_ids = set(all_links)
        else:
            s = await resolve_object(nb, (await get_schema(ctx)).resolve_type("dcim.sites"), site)  # type: ignore[arg-type]
            devices, _ = await nb.list(
                "dcim/devices/", {"site_id": s["id"], "fields": "id,name"}, limit=MAX_NODES
            )
            names.update({d["id"]: label(d) or "?" for d in devices})
            all_links = dict(await asyncio.gather(*(links_of(d["id"]) for d in devices)))
            scope_ids = set(all_links)

        info: dict[int, dict[str, Any]] = {}
        ids = sorted(names)
        if ids:
            devs, _ = await nb.list(
                "dcim/devices/", {"id": ids, "fields": "id,name,role,site"}, limit=len(ids)
            )
            info = {d["id"]: d for d in devs}

        nodes: dict[str, str] = {}
        external: set[str] = set()
        seen: set[frozenset[tuple[str, str]]] = set()
        edges: list[dict[str, Any]] = []
        mermaid_edges: list[tuple[str, str, str]] = []
        for dev_id, links in all_links.items():
            here = names[dev_id]
            nodes.setdefault(here, label((info.get(dev_id) or {}).get("role")) or "")
            for link in links:
                peer = link.peer_name
                if link.peer_kind == "device":
                    nodes.setdefault(peer, label((info.get(link.peer_id or 0) or {}).get("role")) or "")
                    if link.peer_id not in scope_ids:
                        external.add(peer)  # drawn but not expanded
                else:
                    nodes.setdefault(peer, link.peer_kind.split(".")[-1])
                    external.add(peer)
                key = frozenset({(here, link.local_if), (peer, link.peer_if or "")})
                if key in seen:
                    continue
                seen.add(key)
                edge = compact(
                    {
                        "a": f"{here} {link.local_if}",
                        "b": f"{peer} {link.peer_if or ''}".strip(),
                        "via": link.via,
                    }
                )
                edges.append(edge)
                lbl = short_if(link.local_if) + (f" - {short_if(link.peer_if)}" if link.peer_if else "")
                mermaid_edges.append((here, peer, lbl))
        out: dict[str, Any] = {
            "scope": f"{hops} hop(s) from {names.get(next(iter(scope_ids), 0), '?')}"
            if device is not None
            else "site",
            "node_count": len(nodes),
            "edge_count": len(edges),
            "mermaid": mermaid(nodes, mermaid_edges, external),
            "edges": edges,
        }
        if len(names) >= MAX_NODES:
            out["note"] = f"Stopped at {MAX_NODES} devices; use fewer hops."
        return out

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_impact_analysis(
        ctx: Context, device: Annotated[str | int, Field(description="Device name or id to take down.")]
    ) -> dict[str, Any]:
        """What's affected if a device goes down: neighbours (flagging single-homed ones that would be
        cut off), VMs it hosts (and whether it's its cluster's only host), circuits and tunnels
        terminating on it, subnets it's the likely gateway for without FHRP, devices that lose power
        (if it's a PDU), and services on it and its VMs."""
        nb = get_netbox(ctx)
        _, dev = await resolve_host(nb, device, "device")
        dev_id = dev["id"]
        links, (vms, vm_total), (ips, _), (outlets, _), (services, svc_total) = await asyncio.gather(
            device_links(nb, dev_id),
            nb.list(
                "virtualization/virtual-machines/",
                {"device_id": dev_id, "fields": "id,name,status"},
                limit=500,
            ),
            nb.list("ipam/ip-addresses/", {"device_id": dev_id, "fields": "id,address,vrf,role"}, limit=500),
            nb.list(
                "dcim/power-outlets/",
                {"device_id": dev_id, "fields": "id,name,connected_endpoints"},
                limit=500,
            ),
            nb.list("ipam/services/", {"device_id": dev_id, "brief": "true"}, limit=100),
        )

        # Neighbours, and whether each has any other way out.
        by_neighbour: dict[int, list[Link]] = defaultdict(list)
        for link in links:
            if link.peer_id is not None:
                by_neighbour[link.peer_id].append(link)
        limiter = asyncio.Semaphore(PARALLEL)

        async def other_paths(nid: int) -> bool:
            async with limiter:
                return any(
                    lk.peer_id not in (None, dev_id) or lk.peer_kind != "device"
                    for lk in await device_links(nb, nid)
                )

        redundant = dict(
            zip(by_neighbour, await asyncio.gather(*(other_paths(n) for n in by_neighbour)), strict=True)
        )
        neighbours = [
            {
                "device": ls[0].peer_name,
                "links": [f"{short_if(lk.local_if)} - {short_if(lk.peer_if or '?')}" for lk in ls],
                "impact": "has other connections"
                if redundant[nid]
                else "SINGLE-HOMED: loses all connectivity",
            }
            for nid, ls in by_neighbour.items()
        ]
        external = [
            compact({"interface": lk.local_if, "endpoint": lk.peer_name, "type": lk.peer_kind, "via": lk.via})
            for lk in links
            if lk.peer_kind != "device"
        ]

        # Tunnels on its interfaces; cluster role.
        all_ifaces, _ = await nb.list("dcim/interfaces/", {"device_id": dev_id, "fields": "id"}, limit=2000)
        tunnels: list[str] = []
        if all_ifaces:
            terms, _ = await nb.list(
                "vpn/tunnel-terminations/", {"interface_id": [i["id"] for i in all_ifaces]}, limit=200
            )
            tunnels = sorted({label(t.get("tunnel")) or "?" for t in terms})
        cluster_note = None
        if dev.get("cluster"):
            hosts, _ = await nb.list(
                "dcim/devices/", {"cluster_id": dev["cluster"]["id"], "fields": "id"}, limit=100
            )
            if len(hosts) <= 1:
                cluster_note = f"Only host of cluster {label(dev['cluster'])}: all its VMs go down."
            else:
                cluster_note = (
                    f"One of {len(hosts)} hosts in cluster {label(dev['cluster'])}: VMs may fail over."
                )

        gateways = await _gateway_subnets(nb, ips)
        powered = await _powered_devices(nb, dev_id, outlets)
        vm_services = 0
        if vms:
            _, vm_services = await nb.list(
                "ipam/services/", {"virtual_machine_id": [v["id"] for v in vms]}, limit=1
            )

        single_homed = sorted(ls[0].peer_name for nid, ls in by_neighbour.items() if not redundant[nid])
        return compact_dict(
            {
                "device": label(dev),
                "summary": {
                    "neighbours": len(neighbours),
                    "single_homed_neighbours": single_homed,
                    "vms": vm_total,
                    "circuits_and_external": len(external),
                    "tunnels": len(tunnels),
                    "gateway_subnets_without_fhrp": len([g for g in gateways if "no FHRP" in g]),
                    "devices_losing_power": len([p for p in powered if p.endswith("loses power")]),
                    "services": svc_total + vm_services,
                },
                "neighbours": neighbours,
                "external": external,
                "tunnels": tunnels,
                "vms": [f"{label(v)} ({choice(v.get('status'))})" for v in vms[:50]],
                "cluster": cluster_note,
                "gateway_subnets": gateways,
                "powered_devices": powered,
            }
        )

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_compare(
        ctx: Context,
        object_type: Annotated[
            str, Field(description="Type of both objects, e.g. 'devices', 'vlans', 'prefixes'.")
        ],
        a: Annotated[
            str | int,
            Field(description="First object: name / address / prefix / id, or field=value (e.g. 'vid=10')."),
        ],
        b: Annotated[str | int, Field(description="Second object.")],
        include_interfaces: Annotated[
            bool, Field(description="For devices: also compare interfaces by name.")
        ] = True,
    ) -> dict[str, Any]:
        """Field-by-field differences between two objects of the same type (e.g. the two switches in a
        pair). For devices, also compares interfaces by name: only on A, only on B, and differences in
        type, enabled, mode, VLANs, MTU, LAG, VRF."""
        nb = get_netbox(ctx)
        otype = (await get_schema(ctx)).resolve_type(object_type)
        obj_a, obj_b = await asyncio.gather(resolve_object(nb, otype, a), resolve_object(nb, otype, b))
        # Flatten field values only: the objects themselves have id + display too.
        flat_a = {k: _flatten(v) for k, v in compact(obj_a).items()}
        flat_b = {k: _flatten(v) for k, v in compact(obj_b).items()}
        differences, same = compare_dicts(flat_a, flat_b, _COMPARE_SKIP | {"config_context"})
        out: dict[str, Any] = {
            "object_type": otype.key,
            "a": label(obj_a),
            "b": label(obj_b),
            "differences": differences,
            "identical_fields": same,
        }
        if otype.key == "dcim.devices" and include_interfaces:
            (ifs_a, _), (ifs_b, _) = await asyncio.gather(
                nb.list("dcim/interfaces/", {"device_id": obj_a["id"]}, limit=2000),
                nb.list("dcim/interfaces/", {"device_id": obj_b["id"]}, limit=2000),
            )
            by_a = {i["name"]: _iface_profile(i) for i in ifs_a}
            by_b = {i["name"]: _iface_profile(i) for i in ifs_b}
            differing = {}
            for name in sorted(set(by_a) & set(by_b)):
                diff, _ = compare_dicts(by_a[name], by_b[name], set())
                if diff:
                    differing[name] = diff
            out["interfaces"] = compact(
                {
                    "only_on_a": sorted(set(by_a) - set(by_b)),
                    "only_on_b": sorted(set(by_b) - set(by_a)),
                    "same_name_count": len(set(by_a) & set(by_b)),
                    "differing": dict(list(differing.items())[:50]),
                }
            )
        return compact_dict(out)


async def _gateway_subnets(nb: NetBoxClient, ips: list[dict[str, Any]]) -> list[str]:
    """Subnets where this device holds the first usable address (a likely gateway), and whether an
    FHRP/VIP address exists there for redundancy."""
    out = []
    for ip in ips:
        iface = ipaddress.ip_interface(ip["address"])
        net = iface.network
        if net.num_addresses <= 2 or iface.ip != next(net.hosts(), None):
            continue
        vrf = (ip.get("vrf") or {}).get("id")
        peers, _ = await nb.list(
            "ipam/ip-addresses/",
            {"parent": str(net), "vrf_id": vrf or "null", "fields": "id,role"},
            limit=500,
        )
        fhrp = any(choice(p.get("role")) in FHRP_ROLES for p in peers)
        vrf_label = label(ip.get("vrf")) or "global"
        out.append(f"{net} ({vrf_label}): {'FHRP/VIP present' if fhrp else 'likely gateway, no FHRP'}")
    return out


async def _powered_devices(nb: NetBoxClient, dev_id: int, outlets: list[dict[str, Any]]) -> list[str]:
    """Devices fed from this device's outlets, and whether they have another power source."""
    fed: dict[int, str] = {}
    for o in outlets:
        for port in o.get("connected_endpoints") or []:
            if port.get("device"):
                fed[port["device"]["id"]] = label(port["device"]) or "?"
    if not fed:
        return []
    ports, _ = await nb.list(
        "dcim/power-ports/", {"device_id": sorted(fed), "fields": "id,device,connected_endpoints"}, limit=2000
    )
    other_sources: dict[int, bool] = defaultdict(bool)
    for p in ports:
        for upstream in p.get("connected_endpoints") or []:
            owner = (upstream.get("device") or {}).get("id")
            if owner != dev_id:
                other_sources[p["device"]["id"]] = True
    return [
        f"{name}: {'has another power source' if other_sources[did] else 'loses power'}"
        for did, name in sorted(fed.items(), key=lambda x: x[1])
    ]
