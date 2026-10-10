"""Shared lookups for the curated tools: resolve human references (names, addresses) to objects."""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Literal

from netbox_mcp.client import NetBoxClient
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.schema import ObjectType

HostKind = Literal["device", "vm"]


def label(obj: dict[str, Any] | int | str | None) -> str | None:
    """Short human label for a (possibly nested) NetBox object. Bare ids/strings pass through."""
    if obj is None or obj == "":
        return None
    if not isinstance(obj, dict):
        return str(obj)
    return str(
        obj.get("display") or obj.get("name") or obj.get("address") or obj.get("prefix") or obj.get("id")
    )


def choice(v: Any) -> Any:
    return v.get("value") if isinstance(v, dict) and "value" in v else v


def _ambiguous(what: str, ref: Any, objs: list[dict[str, Any]], detail: str) -> InvalidRequest:
    options = "; ".join(f"id={o['id']} ({label(o.get(detail)) or '-'})" for o in objs[:10])
    return InvalidRequest(f"{what} {ref!r} is ambiguous: {options}. Pass the id instead.")


def _host_path(kind: HostKind) -> str:
    return "dcim/devices/" if kind == "device" else "virtualization/virtual-machines/"


def iface_path(kind: HostKind) -> str:
    return "dcim/interfaces/" if kind == "device" else "virtualization/interfaces/"


async def resolve_host(
    nb: NetBoxClient, ref: str | int, kind: HostKind | None = None
) -> tuple[HostKind, dict[str, Any]]:
    """Find a device or virtual machine by id or exact name (devices are tried first)."""
    if isinstance(ref, int) or str(ref).isdigit():
        k: HostKind = kind or "device"  # a bare id is a device unless kind="vm"
        return k, await nb.get_object(f"{_host_path(k)}{int(ref)}/")
    kinds: list[HostKind] = [kind] if kind else ["device", "vm"]
    for k in kinds:
        objs, count = await nb.list(_host_path(k), {"name": ref}, limit=10)
        if count == 1:
            return k, objs[0]
        if count > 1:
            raise _ambiguous("Name", ref, objs, "site")
    hint = await _similar_names(nb, str(ref))
    raise InvalidRequest(f"No device or virtual machine named {ref!r}.{hint}")


async def _similar_names(nb: NetBoxClient, ref: str) -> str:
    names: list[str] = []
    for path in ("dcim/devices/", "virtualization/virtual-machines/"):
        objs, _ = await nb.list(path, {"name__ic": ref, "brief": "true"}, limit=5)
        names += [label(o) or "" for o in objs]
    return f" Similar: {', '.join(names)}." if names else " Try netbox_search."


async def resolve_interface(
    nb: NetBoxClient,
    *,
    host: str | int | None = None,
    interface: str | None = None,
    interface_id: int | None = None,
    kind: HostKind | None = None,
) -> tuple[HostKind, dict[str, Any]]:
    """Find an interface by id, or by host (device/VM) + interface name."""
    if interface_id is not None:
        k: HostKind = kind or "device"
        return k, await nb.get_object(f"{iface_path(k)}{interface_id}/")
    if host is None or not interface:
        raise InvalidRequest("Pass interface_id, or both device and interface name.")
    hk, h = await resolve_host(nb, host, kind)
    path = iface_path(hk)
    filt = {"device_id": h["id"]} if hk == "device" else {"virtual_machine_id": h["id"]}
    objs, _ = await nb.list(path, {**filt, "name": interface}, limit=2)
    if not objs:
        # Tolerate abbreviations like Gi1/0/1 for GigabitEthernet1/0/1.
        objs = [o for o in (await nb.list(path, filt, limit=1000))[0] if _abbrev_match(interface, o["name"])]
    if len(objs) != 1:
        raise InvalidRequest(
            f"{'No' if not objs else 'Ambiguous'} interface {interface!r} on {label(h)}."
            + (f" Matches: {', '.join(o['name'] for o in objs)}" if objs else "")
        )
    return hk, objs[0]


def _abbrev_match(short: str, full: str) -> bool:
    m = re.fullmatch(r"([A-Za-z-]+)\s*([\d/.:]+)", short.strip())
    f = re.fullmatch(r"([A-Za-z-]+)\s*([\d/.:]+)", full)
    return bool(m and f and m.group(2) == f.group(2) and f.group(1).lower().startswith(m.group(1).lower()))


async def resolve_vrf(nb: NetBoxClient, vrf: str | int | None) -> int | None:
    """VRF by id, name or RD. None means 'any/global' depending on the caller."""
    if vrf is None:
        return None
    if isinstance(vrf, int) or str(vrf).isdigit():
        return int(vrf)
    for filt in ({"name": vrf}, {"rd": vrf}):
        objs, count = await nb.list("ipam/vrfs/", filt, limit=2)
        if count == 1:
            return int(objs[0]["id"])
    raise InvalidRequest(f"No unique VRF named or with RD {vrf!r}.")


async def resolve_prefix(nb: NetBoxClient, prefix: str | int, vrf: str | int | None = None) -> dict[str, Any]:
    if isinstance(prefix, int) or str(prefix).isdigit():
        return await nb.get_object(f"ipam/prefixes/{int(prefix)}/")
    try:
        net = ipaddress.ip_network(str(prefix), strict=False)
    except ValueError as e:
        raise InvalidRequest(f"{prefix!r} is not a valid prefix (e.g. 10.0.0.0/24) or prefix id.") from e
    params: dict[str, Any] = {"prefix": str(net)}
    vrf_id = await resolve_vrf(nb, vrf)
    if vrf_id is not None:
        params["vrf_id"] = vrf_id
    objs, count = await nb.list("ipam/prefixes/", params, limit=10)
    if count == 0:
        raise InvalidRequest(f"Prefix {net} not found{' in that VRF' if vrf_id else ''}.")
    if count > 1:
        raise _ambiguous("Prefix", str(net), objs, "vrf")
    return objs[0]


def normalise_mac(mac: str) -> str:
    digits = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(digits) != 12:
        raise InvalidRequest(f"{mac!r} is not a valid MAC address.")
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2)).upper()


def parse_ip(value: str) -> ipaddress.IPv4Interface | ipaddress.IPv6Interface:
    try:
        return ipaddress.ip_interface(value.strip())
    except ValueError as e:
        raise InvalidRequest(f"{value!r} is not a valid IP address.") from e


# Filters tried, in order, to find an object by a human reference (whichever the type supports).
_NATURAL_KEYS = ("name", "address", "prefix", "label", "cid", "slug", "model", "vid")


def content_type(otype: ObjectType) -> str:
    """NetBox content-type label for an object type, e.g. dcim.devices -> 'dcim.device',
    ipam.ip-addresses -> 'ipam.ipaddress' (as used by changelog, journal and generic relations)."""
    app = otype.key.split(".", 1)[0]
    model = (otype.model or otype.key.split(".", 1)[1].rstrip("s")).replace("-", "")
    return f"{app}.{model.lower()}"


async def resolve_object(nb: NetBoxClient, otype: ObjectType, ref: str | int) -> dict[str, Any]:
    """Find one object of any type by id, or by its natural key (name, address, prefix, label, ...)."""
    if isinstance(ref, int) or str(ref).isdigit():
        return await nb.get_object(f"{otype.path}{int(ref)}/")
    available = otype.filter_names()
    for key in _NATURAL_KEYS:
        if key not in available:
            continue
        objs, count = await nb.list(otype.path, {key: ref}, limit=10)
        if count == 1:
            return objs[0]
        if count > 1:
            options = "; ".join(f"id={o['id']} ({label(o)})" for o in objs)
            raise InvalidRequest(f"{otype.key} {ref!r} is ambiguous: {options}. Pass the id instead.")
    raise InvalidRequest(f"No {otype.key} matching {ref!r}. Try netbox_search, or pass the id.")
