"""Cable path tracing."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.config import Settings
from netbox_mcp.deps import get_netbox
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.tools.common import choice, label, resolve_host, resolve_interface

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

PortType = Literal[
    "interface", "console-port", "console-server-port", "power-port", "power-outlet", "power-feed"
]
# Termination types that end a path (anything else, e.g. front/rear ports, is pass-through).
_PASS_THROUGH = {"front-port", "rear-port"}


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
    ) -> dict[str, Any]:
        """Trace the full cable path from a port to its far end, through patch panels (front/rear
        ports) and circuits. Returns each hop (from -> cable -> to) and whether the path is complete."""
        nb = get_netbox(ctx)
        path = f"dcim/{port_type}s/"
        if port_type == "interface":
            _, origin = await resolve_interface(
                nb, host=device, interface=port, interface_id=port_id, kind="device"
            )
        elif port_id is not None:
            origin = await nb.get_object(f"{path}{port_id}/")
        else:
            if device is None or not port:
                raise InvalidRequest("Pass port_id, or both device and port name.")
            _, dev = await resolve_host(nb, device, "device")
            found, _ = await nb.list(path, {"device_id": dev["id"], "name": port}, limit=2)
            if len(found) != 1:
                raise InvalidRequest(f"No unique {port_type} named {port!r} on {label(dev)}.")
            origin = found[0]

        segments: list[Any] = await nb.get(f"{path}{origin['id']}/trace/") or []
        origin_label = f"{label(origin.get('device'))} {origin.get('name')}"
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
        complete = (
            bool(segments[-1][1])
            and bool(last_far)
            and all(_term_type(t) not in _PASS_THROUGH for t in last_far)
        )
        out: dict[str, Any] = {
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
