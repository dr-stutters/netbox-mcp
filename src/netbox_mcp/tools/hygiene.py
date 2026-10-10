"""Data-hygiene report: find common data-quality problems in NetBox."""

from __future__ import annotations

import asyncio
import ipaddress
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, get_args

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.deps import get_netbox, get_schema
from netbox_mcp.tools.common import choice, label, resolve_object
from netbox_mcp.tools.ipam import usable_size

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

Check = Literal[
    "duplicate_ips",
    "ips_outside_prefixes",
    "prefixes_over_threshold",
    "prefixes_unused",
    "devices_without_primary_ip",
    "devices_without_serial",
    "devices_unpositioned_in_rack",
    "vlans_unused",
    "trunks_without_cable",
    "cabled_interfaces_without_description",
]
ALL_CHECKS: tuple[str, ...] = get_args(Check)

DESCRIPTIONS = {
    "duplicate_ips": "The same IP address recorded more than once in a VRF (shared-address roles excluded)",
    "ips_outside_prefixes": "IP addresses not inside any prefix in their VRF",
    "prefixes_over_threshold": "Active prefixes at or above the utilisation threshold",
    "prefixes_unused": "Active prefixes with no IP addresses and no child prefixes",
    "devices_without_primary_ip": "Active devices with interfaces but no primary IP",
    "devices_without_serial": "Active devices with no serial number",
    "devices_unpositioned_in_rack": "Devices assigned to a rack but without a unit position",
    "vlans_unused": "VLANs with no prefixes and no interfaces carrying them",
    "trunks_without_cable": "Enabled physical trunk (tagged) interfaces with no cable",
    "cabled_interfaces_without_description": "Cabled physical interfaces with no description",
}

# Roles whose addresses are legitimately shared by several interfaces.
SHARED_ROLES = {"anycast", "vip", "vrrp", "hsrp", "glbp", "carp"}
MAX_OBJECTS = 20_000  # per object type fetched for local analysis

Net = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass
class Finding:
    count: int
    examples: list[str] = field(default_factory=list)


# --- IP / prefix analysis (pure functions over fetched data) --------------------------------


def _vrf_key(obj: dict[str, Any]) -> str:
    return label(obj.get("vrf")) or "global"


def find_duplicate_ips(ips: list[dict[str, Any]]) -> list[str]:
    seen: dict[tuple[str, str], list[int]] = defaultdict(list)
    for ip in ips:
        if choice(ip.get("role")) in SHARED_ROLES:
            continue
        host = str(ipaddress.ip_interface(ip["address"]).ip)
        seen[(_vrf_key(ip), host)].append(ip["id"])
    return [
        f"{host} ({vrf}): ids {', '.join(map(str, ids))}"
        for (vrf, host), ids in sorted(seen.items())
        if len(ids) > 1
    ]


class PrefixIndex:
    """Prefixes grouped by VRF and length, for fast 'which prefixes contain this address' lookups."""

    def __init__(self, prefixes: list[dict[str, Any]]) -> None:
        self.by_vrf: dict[str, dict[int, dict[int, list[dict[str, Any]]]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        for p in prefixes:
            net = ipaddress.ip_network(p["prefix"])
            self.by_vrf[_vrf_key(p)][net.prefixlen].setdefault(int(net.network_address), []).append(p)

    def containing(
        self, vrf: str, address: ipaddress.IPv4Address | ipaddress.IPv6Address
    ) -> list[dict[str, Any]]:
        out = []
        max_len = 32 if address.version == 4 else 128
        for length, nets in self.by_vrf.get(vrf, {}).items():
            if length > max_len:
                continue
            base = int(address) >> (max_len - length) << (max_len - length)
            for p in nets.get(base, []):
                if ipaddress.ip_network(p["prefix"]).version == address.version:
                    out.append(p)
        return out


def analyse_prefixes(
    prefixes: list[dict[str, Any]], ips: list[dict[str, Any]], threshold: int
) -> dict[str, list[str]]:
    """IPs outside any prefix, prefixes over the threshold, and unused prefixes."""
    index = PrefixIndex(prefixes)
    ip_counts: dict[int, int] = defaultdict(int)
    outside = []
    for ip in ips:
        iface = ipaddress.ip_interface(ip["address"])
        parents = index.containing(_vrf_key(ip), iface.ip)
        if not parents:
            outside.append(f"{ip['address']} ({_vrf_key(ip)})")
        for p in parents:
            ip_counts[p["id"]] += 1

    nets = {p["id"]: ipaddress.ip_network(p["prefix"]) for p in prefixes}
    children: dict[int, list[Net]] = defaultdict(list)
    for p in prefixes:
        for q in prefixes:
            if p["id"] == q["id"] or nets[q["id"]].version != nets[p["id"]].version:
                continue
            # Global containers count children from any VRF (as NetBox does); others only their own.
            same_vrf = p.get("vrf") is None or _vrf_key(p) == _vrf_key(q)
            if same_vrf and nets[q["id"]] != nets[p["id"]] and nets[q["id"]].subnet_of(nets[p["id"]]):  # type: ignore[arg-type]
                children[p["id"]].append(nets[q["id"]])

    over, unused = [], []
    for p in prefixes:
        status = choice(p.get("status"))
        if status not in ("active", "container") or p.get("mark_utilized"):
            continue
        net = nets[p["id"]]
        if status == "container":
            covered = _covered(children[p["id"]])
            pct = 100 * covered / net.num_addresses
        else:
            size = usable_size(net, bool(p.get("is_pool")))
            pct = 100 * ip_counts[p["id"]] / size if size else 100.0
            if ip_counts[p["id"]] == 0 and not children[p["id"]]:
                unused.append(f"{net} ({_vrf_key(p)})")
        if pct >= threshold:
            over.append(f"{net} ({_vrf_key(p)}): {pct:.0f}% used")
    return {"ips_outside_prefixes": outside, "prefixes_over_threshold": over, "prefixes_unused": unused}


def _covered(nets: list[Net]) -> int:
    v4 = [n for n in nets if isinstance(n, ipaddress.IPv4Network)]
    v6 = [n for n in nets if isinstance(n, ipaddress.IPv6Network)]
    return sum(n.num_addresses for n in ipaddress.collapse_addresses(v4)) + sum(
        n.num_addresses for n in ipaddress.collapse_addresses(v6)
    )


# --- server-side checks ---------------------------------------------------------------------


def _device_label(d: dict[str, Any]) -> str:
    site = label(d.get("site"))
    return f"{label(d)} ({site})" if site else label(d) or "?"


def _iface_label(i: dict[str, Any]) -> str:
    return f"{label(i.get('device'))} {i.get('name')}"


async def _query(
    nb: NetBoxClient, path: str, params: dict[str, Any], sample: int, fmt: Callable[[dict[str, Any]], str]
) -> Finding:
    items, count = await nb.list(path, params, limit=sample)
    return Finding(count, [fmt(i) for i in items])


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def netbox_data_hygiene(
        ctx: Context,
        checks: Annotated[
            list[Check] | None, Field(description="Checks to run (default: all). See the tool description.")
        ] = None,
        site: Annotated[
            str | int | None,
            Field(description="Limit to one site (name/slug/id). IP checks then cover the site's prefixes."),
        ] = None,
        prefix_threshold: Annotated[
            int, Field(description="Utilisation % that counts as nearly full.", ge=1, le=100)
        ] = 90,
        sample: Annotated[int, Field(description="Example offenders to list per check.", ge=1, le=100)] = 10,
    ) -> dict[str, Any]:
        """Find data-quality problems: duplicate IPs, IPs outside any prefix, nearly full or unused
        prefixes, active devices without a primary IP or serial, rack devices without a position,
        unused VLANs, trunk ports without cables, and cabled interfaces without descriptions.
        Returns each failing check with a count and example offenders, plus the checks that passed."""
        nb = get_netbox(ctx)
        selected = list(dict.fromkeys(checks or ALL_CHECKS))
        scope: dict[str, Any] = {}
        scope_label = "all sites"
        notes: list[str] = []
        if site is not None:
            s = await resolve_object(nb, (await get_schema(ctx)).resolve_type("dcim.sites"), site)
            scope = {"site_id": s["id"]}
            scope_label = f"site {label(s)}"

        results: dict[str, Finding] = {}
        tasks: dict[str, Awaitable[Finding]] = {}
        active_devices = {"status": "active", **scope, "fields": "id,name,site"}
        if "devices_without_primary_ip" in selected:
            tasks["devices_without_primary_ip"] = _query(
                nb,
                "dcim/devices/",
                {**active_devices, "has_primary_ip": "false", "interface_count__gt": 0},
                sample,
                _device_label,
            )
        if "devices_without_serial" in selected:
            tasks["devices_without_serial"] = _query(
                nb, "dcim/devices/", {**active_devices, "serial__empty": "true"}, sample, _device_label
            )
        if "trunks_without_cable" in selected:
            tasks["trunks_without_cable"] = _query(
                nb,
                "dcim/interfaces/",
                {
                    **scope,
                    "kind": "physical",
                    "mode": ["tagged", "tagged-all"],
                    "enabled": "true",
                    "cabled": "false",
                    "mark_connected": "false",
                    "device_status": "active",
                },
                sample,
                _iface_label,
            )
        if "cabled_interfaces_without_description" in selected:
            tasks["cabled_interfaces_without_description"] = _query(
                nb,
                "dcim/interfaces/",
                {**scope, "kind": "physical", "cabled": "true", "description__empty": "true"},
                sample,
                _iface_label,
            )

        async def unpositioned() -> Finding:
            devices, _ = await nb.list(
                "dcim/devices/",
                {**scope, "position__empty": "true", "fields": "id,name,site,rack"},
                limit=MAX_OBJECTS,
            )
            hits = [d for d in devices if d.get("rack")]
            return Finding(len(hits), [f"{label(d)} (rack {label(d.get('rack'))})" for d in hits[:sample]])

        if "devices_unpositioned_in_rack" in selected:
            tasks["devices_unpositioned_in_rack"] = unpositioned()

        ip_checks = {"duplicate_ips", "ips_outside_prefixes", "prefixes_over_threshold", "prefixes_unused"}
        needs_prefixes = bool(ip_checks & set(selected)) or "vlans_unused" in selected
        prefixes: list[dict[str, Any]] = []
        if needs_prefixes:
            prefix_fields = "id,prefix,vrf,status,mark_utilized,is_pool,vlan"
            prefixes, prefix_total = await nb.list(
                "ipam/prefixes/", {**scope, "fields": prefix_fields}, limit=MAX_OBJECTS
            )
            if prefix_total > len(prefixes):
                notes.append(f"Only the first {len(prefixes)} of {prefix_total} prefixes were analysed.")

        if ip_checks & set(selected):
            ip_fields = {"fields": "id,address,vrf,role,status"}
            if scope:
                # IPs inside the site's prefixes (deduplicated across nested prefixes).
                roots = [p for p in prefixes if not any(_contains(q, p) for q in prefixes if q is not p)]
                batches = await asyncio.gather(
                    *(
                        nb.list(
                            "ipam/ip-addresses/",
                            {
                                **ip_fields,
                                "parent": p["prefix"],
                                "vrf_id": (p.get("vrf") or {}).get("id") or "null",
                            },
                            limit=MAX_OBJECTS,
                        )
                        for p in roots
                    )
                )
                ips = list({ip["id"]: ip for batch, _ in batches for ip in batch}.values())
            else:
                ips, ip_total = await nb.list("ipam/ip-addresses/", ip_fields, limit=MAX_OBJECTS)
                if ip_total > len(ips):
                    notes.append(f"Only the first {len(ips)} of {ip_total} IP addresses were analysed.")
            if "duplicate_ips" in selected:
                dups = find_duplicate_ips(ips)
                results["duplicate_ips"] = Finding(len(dups), dups[:sample])
            analysis = analyse_prefixes(prefixes, ips, prefix_threshold)
            for name, hits in analysis.items():
                if name in selected:
                    results[name] = Finding(len(hits), hits[:sample])

        if "vlans_unused" in selected:
            results["vlans_unused"] = await _unused_vlans(nb, scope, prefixes, sample)

        for name, finding in zip(tasks, await asyncio.gather(*tasks.values()), strict=True):
            results[name] = finding

        problems = {
            name: {"description": DESCRIPTIONS[name], "count": f.count, "examples": f.examples}
            for name in selected
            if (f := results[name]).count
        }
        out: dict[str, Any] = {
            "scope": scope_label,
            "checks_run": len(selected),
            "issues_total": sum(p["count"] for p in problems.values()),
            "problems": problems,
            "passed": [name for name in selected if not results[name].count],
        }
        if "prefixes_over_threshold" in selected:
            out["prefix_threshold_pct"] = prefix_threshold
        if notes:
            out["notes"] = notes
        return out


def _contains(outer: dict[str, Any], inner: dict[str, Any]) -> bool:
    a, b = ipaddress.ip_network(outer["prefix"]), ipaddress.ip_network(inner["prefix"])
    return a.version == b.version and a != b and b.subnet_of(a) and _vrf_key(outer) == _vrf_key(inner)  # type: ignore[arg-type]


async def _unused_vlans(
    nb: NetBoxClient, scope: dict[str, Any], prefixes: list[dict[str, Any]], sample: int
) -> Finding:
    vlans, _ = await nb.list("ipam/vlans/", {**scope, "brief": "true"}, limit=MAX_OBJECTS)
    if scope:
        groups, _ = await nb.list(
            "ipam/vlan-groups/",
            {"scope_type": "dcim.site", "scope_id": scope["site_id"], "brief": "true"},
            limit=200,
        )
        if groups:
            grouped, _ = await nb.list(
                "ipam/vlans/", {"group_id": [g["id"] for g in groups], "brief": "true"}, limit=MAX_OBJECTS
            )
            vlans = list({v["id"]: v for v in [*vlans, *grouped]}.values())
    with_prefix = {(p.get("vlan") or {}).get("id") for p in prefixes}
    candidates = [v for v in vlans if v["id"] not in with_prefix]
    limiter = asyncio.Semaphore(8)

    async def carried(v: dict[str, Any]) -> bool:
        async with limiter:
            (_, n_dev), (_, n_vm) = await asyncio.gather(
                nb.list("dcim/interfaces/", {"vlan_id": v["id"], "brief": "true"}, limit=1),
                nb.list("virtualization/interfaces/", {"vlan_id": v["id"], "brief": "true"}, limit=1),
            )
        return bool(n_dev or n_vm)

    used = await asyncio.gather(*(carried(v) for v in candidates[:500]))
    unused = [v for v, is_used in zip(candidates, used, strict=False) if not is_used]
    return Finding(
        len(unused),
        [
            f"{v.get('vid')} {v.get('name')} ({label(v.get('group')) or label(v.get('site')) or 'global'})"
            for v in unused[:sample]
        ],
    )
