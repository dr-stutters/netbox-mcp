"""Cable path tracing and far-end lookups."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.deps import get_netbox
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.formatting import compact, compact_dict
from netbox_mcp.tools.common import choice, label, resolve_host, resolve_interface

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

TracePort = Literal[
    "interface", "console-port", "console-server-port", "power-port", "power-outlet", "power-feed"
]
PathPort = Literal["front-port", "rear-port", "circuit-termination"]
PortType = Literal[TracePort, PathPort]
# Termination types a path passes through rather than ends at.
_PASS_THROUGH = {"front-port", "rear-port"}
# Where each port type lives in the API.
_PORT_PATHS = {
    "circuit-termination": "circuits/circuit-terminations/",
}


def _port_path(port_type: str) -> str:
    return _PORT_PATHS.get(port_type, f"dcim/{port_type}s/")


def _term_type(t: dict[str, Any]) -> str:
    """Component type from its API URL, e.g. .../api/dcim/front-ports/1/ -> 'front-port'."""
    m = re.search(r"/api/[a-z-]+/([a-z-]+)/\d+/?$", t.get("url") or "")
    if not m:
        return "unknown"
    name = m.group(1)
    return name[:-1] if name.endswith("s") else name


def _term_label(t: dict[str, Any]) -> str:
    owner = t.get("device") or t.get("circuit") or t.get("power_panel") or {}
    name = t.get("name") or (f"term {t['term_side']}" if t.get("term_side") else label(t))
    return f"{' '.join(x for x in (label(owner), name) if x)} ({_term_type(t)})"


def _terms(ts: list[dict[str, Any]] | None) -> str | None:
    return ", ".join(_term_label(t) for t in ts) if ts else None


def _cable(c: dict[str, Any] | None) -> str | None:
    if not c:
        return None
    attrs = ", ".join(str(x) for x in (choice(c.get("type")), choice(c.get("status"))) if x)
    name = c.get("label") or f"cable #{c['id']}"
    return f"{name} ({attrs})" if attrs else name


def _unit(position: Any) -> Any:
    """Rack positions come back as decimals (38.0); show whole units as ints."""
    return int(position) if isinstance(position, float) and position.is_integer() else position


def is_endpoint(terms: list[dict[str, Any]]) -> bool:
    """A path is complete when it ends at something other than a pass-through port."""
    return bool(terms) and all(_term_type(t) not in _PASS_THROUGH for t in terms)


def route(elements: list[list[dict[str, Any]]]) -> list[str]:
    """Flatten a CablePath (alternating termination lists and cable lists) into readable steps."""
    steps = []
    for group in elements:
        if not group:
            continue
        if _term_type(group[0]) == "cable":
            steps.append("cable " + ", ".join(_cable(c) or "?" for c in group))
        else:
            steps.append(_terms(group) or "?")
    return steps


async def _resolve_port(
    nb: NetBoxClient,
    port_type: str,
    device: str | None,
    port: str | None,
    port_id: int | None,
    circuit: str | None,
    side: str,
) -> dict[str, Any]:
    path = _port_path(port_type)
    if port_type == "interface":
        _, origin = await resolve_interface(
            nb, host=device, interface=port, interface_id=port_id, kind="device"
        )
        return origin
    if port_id is not None:
        return await nb.get_object(f"{path}{port_id}/")
    if port_type == "circuit-termination":
        if not circuit:
            raise InvalidRequest("Pass port_id, or circuit (its circuit ID) and side (A or Z).")
        circuits, _ = await nb.list("circuits/circuits/", {"cid": circuit}, limit=2)
        if len(circuits) != 1:
            raise InvalidRequest(f"No unique circuit with ID {circuit!r}.")
        found, _ = await nb.list(path, {"circuit_id": circuits[0]["id"], "term_side": side}, limit=1)
        if not found:
            raise InvalidRequest(f"Circuit {circuit} has no {side} termination.")
        return found[0]
    if device is None or not port:
        raise InvalidRequest("Pass port_id, or both device and port name.")
    _, dev = await resolve_host(nb, device, "device")
    found, _ = await nb.list(path, {"device_id": dev["id"], "name": port}, limit=2)
    if len(found) != 1:
        raise InvalidRequest(f"No unique {port_type} named {port!r} on {label(dev)}.")
    return found[0]


def _origin_label(origin: dict[str, Any], port_type: str) -> str:
    if port_type == "circuit-termination":
        return f"{label(origin.get('circuit'))} term {origin.get('term_side')}"
    return f"{label(origin.get('device'))} {origin.get('name')}"


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def netbox_trace_cable(
        ctx: Context,
        device: Annotated[str | None, Field(description="Device name (or id) owning the port.")] = None,
        port: Annotated[
            str | None, Field(description="Port/interface name; interface abbreviations like Gi1/0/1 work.")
        ] = None,
        port_id: Annotated[int | None, Field(description="Port id (instead of device + port).")] = None,
        port_type: Annotated[PortType, Field(description="Kind of port to trace from.")] = "interface",
        circuit: Annotated[
            str | None, Field(description="Circuit ID, for port_type='circuit-termination'.")
        ] = None,
        side: Annotated[Literal["A", "Z"], Field(description="Circuit termination side.")] = "A",
    ) -> dict[str, Any]:
        """Trace cable paths. From an interface, console or power port: the path to its far end, hop by
        hop (from -> cable -> to), through patch panels and circuits. From a patch-panel front/rear
        port or a circuit termination: every path that passes through it, end to end."""
        nb = get_netbox(ctx)
        origin = await _resolve_port(nb, port_type, device, port, port_id, circuit, side)
        origin_label = _origin_label(origin, port_type)
        path = _port_path(port_type)

        if port_type in ("front-port", "rear-port", "circuit-termination"):
            cable_paths: list[dict[str, Any]] = await nb.get(f"{path}{origin['id']}/paths/") or []
            seen: set[tuple[str, ...]] = set()
            paths = []
            for p in cable_paths:
                steps = route(p.get("path") or [])
                key = tuple(sorted((steps[0], steps[-1]))) + (str(len(steps)),) if steps else ()
                if key in seen:
                    continue  # the same run reported in the other direction
                seen.add(key)
                paths.append(
                    {
                        "endpoints": f"{steps[0]} <-> {steps[-1]}" if steps else None,
                        "complete": p.get("is_complete"),
                        "active": p.get("is_active"),
                        "split": p.get("is_split") or None,
                        "route": steps,
                    }
                )
            out: dict[str, Any] = {
                "origin": origin_label,
                "path_count": len(paths),
                "paths": [compact(p) for p in paths],
            }
            if not paths:
                out["note"] = "No cable paths pass through this port."
            return out

        segments: list[Any] = await nb.get(f"{path}{origin['id']}/trace/") or []
        if not segments or not segments[0][1]:
            return {
                "origin": origin_label,
                "cabled": False,
                "hops": [],
                "note": "Port has no cable attached.",
            }

        hops = [
            {"from": _terms(near), "cable": _cable(cable), "to": _terms(far)} for near, cable, far in segments
        ]
        last_far = segments[-1][2] or []
        complete = is_endpoint(last_far)
        out = {
            "origin": origin_label,
            "cabled": True,
            "complete": complete,
            "far_end": _terms(last_far),
            "hop_count": len(hops),
            "hops": [{k: v for k, v in h.items() if v} for h in hops],
        }
        if not complete:
            out["note"] = "Path is incomplete: it ends at a pass-through port or an unconnected cable end."
        return out

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_whats_connected(
        ctx: Context,
        device: Annotated[str | None, Field(description="Device name (or id) owning the interface.")] = None,
        interface: Annotated[
            str | None, Field(description="Interface name; abbreviations like Gi1/0/1 work.")
        ] = None,
        interface_id: Annotated[
            int | None, Field(description="Interface id (instead of device + name).")
        ] = None,
    ) -> dict[str, Any]:
        """What's plugged into this interface, i.e. the far end of its cable path (through patch
        panels). For a device at the far end: its name, interface, role, platform, site, rack position,
        status and primary IP. Also reports circuits / provider networks at the far end."""
        nb = get_netbox(ctx)
        _, iface = await resolve_interface(
            nb, host=device, interface=interface, interface_id=interface_id, kind="device"
        )
        here = f"{label(iface.get('device'))} {iface.get('name')}"
        endpoints = iface.get("connected_endpoints") or []
        if not iface.get("cable") and not endpoints:
            return {"interface": here, "connected": False, "note": "No cable attached."}

        out: dict[str, Any] = {
            "interface": here,
            "connected": bool(endpoints),
            "reachable": iface.get("connected_endpoints_reachable"),
            "cable": label(iface.get("cable")),
            "direct_peer": _terms(iface.get("link_peers")),
        }
        if not endpoints:
            out["note"] = "Cabled, but the path doesn't reach an endpoint (e.g. it stops at a patch panel)."
            return compact_dict(out)

        if iface.get("connected_endpoints_type") == "dcim.interface":
            dev_ids = sorted({e["device"]["id"] for e in endpoints if e.get("device")})
            devices, _ = await nb.list("dcim/devices/", {"id": dev_ids}, limit=len(dev_ids))
            by_id = {d["id"]: d for d in devices}
            far = []
            for e in endpoints:
                d = by_id.get((e.get("device") or {}).get("id"), {})
                far.append(
                    compact(
                        {
                            "device": label(e.get("device")),
                            "interface": e.get("name") or label(e),
                            "role": label(d.get("role")),
                            "platform": label(d.get("platform")),
                            "status": choice(d.get("status")),
                            "site": label(d.get("site")),
                            "location": label(d.get("location")),
                            "rack": label(d.get("rack")),
                            "position": _unit(d.get("position")),
                            "primary_ip": label(d.get("primary_ip")),
                        }
                    )
                )
            out["far_end"] = far
        else:
            out["far_end"] = [
                {"endpoint": _terms([e]), "type": iface.get("connected_endpoints_type")} for e in endpoints
            ]
        peers = {(iface.get("link_peers_type"), p.get("id")) for p in iface.get("link_peers") or []}
        if peers == {(iface.get("connected_endpoints_type"), e.get("id")) for e in endpoints}:
            out.pop("direct_peer", None)  # cabled straight to the far end: nothing extra to say
        return compact_dict(out)
