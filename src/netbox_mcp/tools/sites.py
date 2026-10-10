"""Site summaries, rack elevations and rack-space search."""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.deps import get_netbox, get_schema
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.formatting import compact, compact_dict
from netbox_mcp.tools.common import choice, label, resolve_object

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

MAX_RACKS = 50  # racks inspected per call (each needs its elevation fetched)
PARALLEL_RACKS = 8  # elevation fetches in flight at once

SiteArg = Annotated[str | int, Field(description="Site name, slug or id.")]


# --- rack layout ----------------------------------------------------------------------------


@dataclass
class RackLayout:
    rack: dict[str, Any]
    units: list[int]  # whole unit numbers, top to bottom as NetBox reports them
    front: dict[int, str] = field(default_factory=dict)  # unit -> device label
    rear: dict[int, str] = field(default_factory=dict)
    reserved: dict[int, str] = field(default_factory=dict)  # unit -> reservation description

    def occupied(self, unit: int) -> bool:
        return unit in self.front or unit in self.rear

    def is_free(
        self, unit: int, *, full_depth: bool = True, face: str = "front", use_reserved: bool = False
    ) -> bool:
        if unit in self.reserved and not use_reserved:
            return False
        if full_depth:
            return not self.occupied(unit)
        return unit not in (self.front if face == "front" else self.rear)

    def free_runs(self, **kw: Any) -> list[tuple[int, int]]:
        """Contiguous free unit ranges as (low, high), lowest first."""
        free = sorted(u for u in self.units if self.is_free(u, **kw))
        return runs(free)

    @property
    def used(self) -> int:
        return sum(1 for u in self.units if self.occupied(u))

    @property
    def utilization(self) -> float:
        return round(100 * self.used / len(self.units), 1) if self.units else 0.0


def runs(numbers: list[int]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for n in sorted(numbers):
        if out and n == out[-1][1] + 1:
            out[-1] = (out[-1][0], n)
        else:
            out.append((n, n))
    return out


def fmt_range(low: int, high: int) -> str:
    return f"U{low}" if low == high else f"U{low}-U{high}"


async def rack_layout(
    nb: NetBoxClient, rack: dict[str, Any], limiter: asyncio.Semaphore | None = None
) -> RackLayout:
    async with limiter or asyncio.Semaphore(1):
        (front, _), (rear, _), (reservations, _) = await asyncio.gather(
            nb.list(f"dcim/racks/{rack['id']}/elevation/", {"face": "front"}, limit=1000),
            nb.list(f"dcim/racks/{rack['id']}/elevation/", {"face": "rear"}, limit=1000),
            nb.list("dcim/rack-reservations/", {"rack_id": rack["id"]}, limit=200),
        )
    layout = RackLayout(rack=rack, units=sorted({math.floor(u["id"]) for u in front}, reverse=True))
    # Elevations report half-units (U38, U38.5); a whole unit is taken if either half is.
    for face_units, target in ((front, layout.front), (rear, layout.rear)):
        for u in face_units:
            if u.get("device"):
                target[math.floor(u["id"])] = label(u["device"]) or "?"
    for r in reservations:
        for unit in r.get("units") or []:
            layout.reserved[int(unit)] = r.get("description") or "reserved"
    return layout


def describe_layout(layout: RackLayout) -> dict[str, Any]:
    rack = layout.rack
    # Group each device's units into ranges; note the face for half-depth devices.
    placements: dict[str, dict[str, Any]] = {}
    for unit in layout.units:
        for face, mapping in (("front", layout.front), ("rear", layout.rear)):
            if (dev := mapping.get(unit)) is not None:
                p = placements.setdefault(dev, {"device": dev, "units": set(), "faces": set()})
                p["units"].add(unit)
                p["faces"].add(face)
    devices = []
    for p in sorted(placements.values(), key=lambda p: -max(p["units"])):
        faces = p["faces"]
        devices.append(
            {
                "device": p["device"],
                "units": ", ".join(fmt_range(lo, hi) for lo, hi in reversed(runs(list(p["units"])))),
                "face": "both" if faces == {"front", "rear"} else next(iter(faces)),
            }
        )
    reserved_by_desc: dict[str, list[int]] = {}
    for unit, desc in layout.reserved.items():
        reserved_by_desc.setdefault(desc, []).append(unit)
    return compact_dict(
        {
            "rack": rack.get("name"),
            "id": rack["id"],
            "site": label(rack.get("site")),
            "location": label(rack.get("location")),
            "status": choice(rack.get("status")),
            "u_height": len(layout.units),
            "used_u": layout.used,
            "reserved_u": len(layout.reserved),
            "free_u": len(layout.units)
            - layout.used
            - sum(1 for u in layout.reserved if not layout.occupied(u)),
            "utilization_pct": layout.utilization,
            "devices": devices,
            "reservations": [
                {"units": ", ".join(fmt_range(lo, hi) for lo, hi in runs(units)), "description": desc}
                for desc, units in reserved_by_desc.items()
            ],
            "free_ranges": [fmt_range(lo, hi) for lo, hi in reversed(layout.free_runs())],
        }
    )


async def _resolve_rack(
    nb: NetBoxClient, ctx: Context, rack: str | int, site: str | int | None
) -> dict[str, Any]:
    schema = await get_schema(ctx)
    if isinstance(rack, int) or str(rack).isdigit():
        return await nb.get_object(f"dcim/racks/{int(rack)}/")
    params: dict[str, Any] = {"name": rack}
    if site is not None:
        params["site_id"] = (await resolve_object(nb, schema.resolve_type("dcim.sites"), site))["id"]
    found, count = await nb.list("dcim/racks/", params, limit=10)
    if count == 1:
        return found[0]
    if count > 1:
        options = "; ".join(f"id={r['id']} ({label(r.get('site'))})" for r in found)
        raise InvalidRequest(f"Rack {rack!r} exists in several sites: {options}. Pass site or the id.")
    raise InvalidRequest(f"No rack named {rack!r}{' at that site' if site is not None else ''}.")


# --- tools ----------------------------------------------------------------------------------


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def netbox_rack_elevation(
        ctx: Context,
        rack: Annotated[str | int, Field(description="Rack name or id.")],
        site: Annotated[
            str | int | None,
            Field(description="Site name/slug/id, if the rack name exists in several sites."),
        ] = None,
    ) -> dict[str, Any]:
        """What's in a rack: each device's units (top down) and face, reservations, free unit ranges
        and utilisation. A unit counts as used if a device occupies it on either face."""
        nb = get_netbox(ctx)
        r = await _resolve_rack(nb, ctx, rack, site)
        return describe_layout(await rack_layout(nb, r))

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_find_rack_space(
        ctx: Context,
        units: Annotated[
            int, Field(description="Height of the device to place, in rack units.", ge=1, le=60)
        ],
        site: Annotated[
            str | int | None, Field(description="Site name/slug/id to search (default: all).")
        ] = None,
        location: Annotated[
            str | int | None, Field(description="Location name/slug/id to narrow the search.")
        ] = None,
        full_depth: Annotated[
            bool, Field(description="True: units must be free on front and rear. False: only on `face`.")
        ] = True,
        face: Annotated[str, Field(description="Face for half-depth devices: front or rear.")] = "front",
        include_reserved: Annotated[bool, Field(description="Treat reserved units as available.")] = False,
    ) -> dict[str, Any]:
        """Find racks with room for a device of a given height. Returns each rack that fits, the
        lowest starting positions where it fits, and the rack's largest free block."""
        if face not in ("front", "rear"):
            raise InvalidRequest("face must be 'front' or 'rear'.")
        nb = get_netbox(ctx)
        schema = await get_schema(ctx)
        params: dict[str, Any] = {}
        if site is not None:
            params["site_id"] = (await resolve_object(nb, schema.resolve_type("dcim.sites"), site))["id"]
        if location is not None:
            params["location_id"] = (
                await resolve_object(nb, schema.resolve_type("dcim.locations"), location)
            )["id"]
        racks, total = await nb.list("dcim/racks/", params, limit=MAX_RACKS)
        limiter = asyncio.Semaphore(PARALLEL_RACKS)
        layouts = await asyncio.gather(*(rack_layout(nb, r, limiter) for r in racks))

        fits = []
        for layout in layouts:
            free = layout.free_runs(full_depth=full_depth, face=face, use_reserved=include_reserved)
            starts = [p for lo, hi in free for p in range(lo, hi - units + 2)]
            if not starts:
                continue
            largest = max(free, key=lambda r: r[1] - r[0])
            fits.append(
                {
                    "rack": layout.rack.get("name"),
                    "id": layout.rack["id"],
                    "site": label(layout.rack.get("site")),
                    "location": label(layout.rack.get("location")),
                    "fits_at_position": starts[:5],
                    "largest_free_block": fmt_range(*largest),
                    "utilization_pct": layout.utilization,
                }
            )
        out: dict[str, Any] = {
            "units": units,
            "racks_checked": len(racks),
            "racks_with_space": len(fits),
            "results": [compact(f) for f in fits],
        }
        if total > len(racks):
            out["note"] = (
                f"Only the first {len(racks)} of {total} racks were checked; narrow with site/location."
            )
        if not fits:
            out["note"] = (out.get("note", "") + " No rack has enough contiguous free space.").strip()
        return out

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_site_summary(ctx: Context, site: SiteArg) -> dict[str, Any]:
        """Everything at a site in one call: details, locations, racks (with utilisation), device
        counts by role and status, VMs, prefixes, VLANs (direct and site-scoped groups) and circuits."""
        nb = get_netbox(ctx)
        schema = await get_schema(ctx)
        s = await resolve_object(nb, schema.resolve_type("dcim.sites"), site)
        sid = s["id"]
        (
            (locations, _),
            (racks, rack_total),
            (devices, device_total),
            (vms, vm_total),
            (prefixes, prefix_total),
            (vlans_direct, _),
            (groups, _),
            (terminations, circuit_total),
        ) = await asyncio.gather(
            nb.list("dcim/locations/", {"site_id": sid, "brief": "true"}, limit=200),
            nb.list("dcim/racks/", {"site_id": sid}, limit=MAX_RACKS),
            nb.list("dcim/devices/", {"site_id": sid, "fields": "id,name,role,status,rack"}, limit=5000),
            nb.list("virtualization/virtual-machines/", {"site_id": sid, "fields": "id,status"}, limit=5000),
            nb.list("ipam/prefixes/", {"site_id": sid}, limit=100),
            nb.list("ipam/vlans/", {"site_id": sid}, limit=500),
            nb.list(
                "ipam/vlan-groups/", {"scope_type": "dcim.site", "scope_id": sid, "brief": "true"}, limit=100
            ),
            nb.list("circuits/circuit-terminations/", {"site_id": sid}, limit=100),
        )
        vlans_grouped: list[dict[str, Any]] = []
        if groups:
            vlans_grouped, _ = await nb.list(
                "ipam/vlans/", {"group_id": [g["id"] for g in groups]}, limit=500
            )
        vlans = {v["id"]: v for v in [*vlans_direct, *vlans_grouped]}
        limiter = asyncio.Semaphore(PARALLEL_RACKS)
        layouts = await asyncio.gather(*(rack_layout(nb, r, limiter) for r in racks[:20]))
        utilisation = {layout.rack["id"]: layout.utilization for layout in layouts}
        per_rack = Counter(label(d.get("rack")) for d in devices if d.get("rack"))

        out: dict[str, Any] = {
            "site": compact(
                {
                    k: s.get(k)
                    for k in (
                        "id",
                        "name",
                        "slug",
                        "status",
                        "region",
                        "group",
                        "tenant",
                        "facility",
                        "time_zone",
                        "physical_address",
                        "description",
                    )
                }
            ),
            "counts": {
                "devices": device_total,
                "racks": rack_total,
                "virtual_machines": vm_total,
                "prefixes": prefix_total,
                "vlans": len(vlans),
                "circuits": circuit_total,
            },
            "devices_by_role": dict(Counter(label(d.get("role")) or "none" for d in devices).most_common()),
            "devices_by_status": dict(Counter(choice(d.get("status")) for d in devices).most_common()),
            "locations": [label(loc) for loc in locations],
            "racks": [
                compact(
                    {
                        "rack": r.get("name"),
                        "location": label(r.get("location")),
                        "devices": per_rack.get(r.get("name"), 0),
                        "utilization_pct": utilisation.get(r["id"]),
                    }
                )
                for r in racks
            ],
            "prefixes": [
                compact({k: p.get(k) for k in ("prefix", "status", "vrf", "vlan", "description")})
                for p in prefixes
            ],
            "vlans": sorted(f"{v.get('vid')} {v.get('name')}" for v in vlans.values()),
            "circuits": [
                compact(
                    {
                        "circuit": label(t.get("circuit")),
                        "side": t.get("term_side"),
                        "location": label(t.get("termination")),
                    }
                )
                for t in terminations
            ],
        }
        if vms:
            out["vms_by_status"] = dict(Counter(choice(v.get("status")) for v in vms).most_common())
        if device_total <= 50:
            out["devices"] = sorted(label(d) or "?" for d in devices)
        return compact_dict(out)
