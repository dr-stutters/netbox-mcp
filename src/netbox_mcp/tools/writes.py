"""Write tools: generic create/update/delete, allocation, cabling and journal entries.

Only registered when NETBOX_MCP_ENABLE_WRITES=true. Every tool is preview-first: called without
`confirm` it returns what it would do plus a single-use token; called again with identical arguments
and confirm=<token> it applies exactly that (see netbox_mcp.preview)."""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.deps import current_caller, get_netbox, get_previews, get_schema
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.formatting import compact
from netbox_mcp.preview import Plan, preview_or_apply
from netbox_mcp.schema import ObjectType, SchemaIndex
from netbox_mcp.tools.cabling import PortType, is_endpoint, port_path, resolve_port, terms_label
from netbox_mcp.tools.common import (
    content_type,
    label,
    resolve_interface,
    resolve_ip_pool,
    resolve_object,
    resolve_prefix,
)
from netbox_mcp.tools.generic import ObjectTypeArg
from netbox_mcp.tools.history import RefLabeller
from netbox_mcp.tools.ipam import next_child_prefixes

audit = logging.getLogger("netbox_mcp.audit")

CREATE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
UPDATE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)
DELETE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)

MAX_BULK = 500
_SLUG_RE = re.compile(r"^[a-z0-9_-]+$")

TagsArg = Annotated[
    list[str] | None,
    Field(description="Tags to apply, by slug or name (the tags must already exist)."),
]
ConfirmArg = Annotated[
    str | None,
    Field(
        description="Omit to get a preview and a confirm token. To apply, repeat the call with exactly the "
        "same arguments plus confirm=<token from the preview>."
    ),
]

# Component counters on a device, reported when previewing its deletion.
_DEVICE_COMPONENTS = (
    ("interface_count", "interfaces"),
    ("front_port_count", "front ports"),
    ("rear_port_count", "rear ports"),
    ("console_port_count", "console ports"),
    ("console_server_port_count", "console server ports"),
    ("power_port_count", "power ports"),
    ("power_outlet_count", "power outlets"),
    ("module_bay_count", "module bays"),
    ("device_bay_count", "device bays"),
    ("inventory_item_count", "inventory items"),
)


def _normalise(data: dict[str, Any]) -> dict[str, Any]:
    """Convenience: allow tags as plain strings (slug or name)."""
    tags = data.get("tags")
    if isinstance(tags, list):
        data = {
            **data,
            "tags": [
                ({"slug": t} if _SLUG_RE.match(t) else {"name": t}) if isinstance(t, str) else t for t in tags
            ],
        }
    return data


def _ids(objs: Any) -> list[Any]:
    return [o.get("id") for o in (objs if isinstance(objs, list) else [objs]) if isinstance(o, dict)]


async def _existing(nb: NetBoxClient, otype: ObjectType, ids: list[int]) -> list[dict[str, Any]]:
    """Fetch the objects for ids (full detail), failing if any are missing so nothing is half-applied."""
    found, _ = await nb.list(otype.path, {"id": ids}, limit=len(ids))
    missing = sorted(set(ids) - {o["id"] for o in found})
    if missing:
        raise InvalidRequest(f"No {otype.key} with id(s) {missing}; nothing was changed.")
    return found


def _state(objs: list[dict[str, Any]]) -> list[tuple[Any, Any]]:
    """Fingerprint of objects' current state: any edit bumps last_updated."""
    return sorted((o.get("id"), o.get("last_updated")) for o in objs)


def _current(value: Any, field: str) -> Any:
    """A current field value in the shape it's written: references -> id, choices -> value, tags -> slugs."""
    if field == "tags" and isinstance(value, list):
        return sorted(t.get("slug") for t in value if isinstance(t, dict))
    if isinstance(value, dict):
        if "value" in value and "label" in value:
            return value["value"]
        if "id" in value and ("url" in value or "display" in value):
            return value["id"]
        return {k: _current(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_current(v, field) for v in value]
    return value


def _requested(value: Any, field: str) -> Any:
    if field == "tags" and isinstance(value, list):
        return sorted(v if isinstance(v, str) else v.get("slug") or v.get("name") for v in value)
    return value


def _shown(value: Any) -> Any:
    """Empty values vanish from compact output, so name them explicitly in a diff."""
    return "(empty)" if value in (None, "", [], {}) else value


def _result(obj: dict[str, Any]) -> dict[str, Any]:
    """An applied object, compacted, without the bulky rendered config context."""
    return {k: v for k, v in compact(obj).items() if k not in ("config_context", "local_context_data")}


def field_changes(obj: dict[str, Any], data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """{field: {from, to}} for fields the update would actually change, plus the unchanged ones."""
    changes: dict[str, Any] = {}
    unchanged: list[str] = []
    for field, new in data.items():
        current = _current(obj.get(field), field)
        wanted = _requested(new, field)
        if isinstance(current, dict) and isinstance(wanted, dict):  # custom_fields: per key
            for key, value in wanted.items():
                if current.get(key) != value:
                    changes[f"{field}.{key}"] = {"from": _shown(current.get(key)), "to": _shown(value)}
            continue
        if current == wanted or (current in (None, "") and wanted in (None, "")):
            unchanged.append(field)
        else:
            changes[field] = {"from": _shown(current), "to": _shown(wanted)}
    return changes, unchanged


async def _labelled(nb: NetBoxClient, schema: SchemaIndex, ct: str, entries: list[dict[str, Any]]) -> None:
    """Replace reference ids in preview entries with 'name (id N)'."""
    for e in entries:
        e.setdefault("object_type", ct)
    await RefLabeller(nb, schema).apply(entries)


async def delete_effects(
    nb: NetBoxClient, otype: ObjectType, objs: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Known side effects of deleting these objects (checked against NetBox 4.7), and blockers."""
    ids = [o["id"] for o in objs]
    effects: list[str] = []
    blockers: list[str] = []

    async def count(path: str, params: dict[str, Any]) -> int:
        return (await nb.list(path, {**params, "brief": "true"}, limit=1))[1]

    if otype.key == "dcim.devices":
        parts = [f"{n} {name}" for f, name in _DEVICE_COMPONENTS if (n := sum(o.get(f) or 0 for o in objs))]
        if parts:
            effects.append("Deletes its components: " + ", ".join(parts))
        if n := await count("ipam/ip-addresses/", {"device_id": ids}):
            effects.append(f"Deletes {n} IP address(es) assigned to its interfaces")
        if n := await count("ipam/services/", {"device_id": ids}):
            effects.append(f"Deletes {n} service(s)")
        if n := await count("dcim/cables/", {"device_id": ids}):
            effects.append(
                f"Leaves {n} cable(s) half-connected (NetBox keeps a cable when one end is deleted)"
            )
        if n := await count("virtualization/virtual-machines/", {"device_id": ids}):
            blockers.append(
                f"{n} VM(s) are hosted on it: NetBox refuses the delete until they're moved or deleted"
            )
    elif otype.key == "virtualization.virtual-machines":
        for path, param, what in (
            ("virtualization/interfaces/", "virtual_machine_id", "interface(s)"),
            ("ipam/ip-addresses/", "virtual_machine_id", "IP address(es)"),
            ("virtualization/virtual-disks/", "virtual_machine_id", "virtual disk(s)"),
            ("ipam/services/", "virtual_machine_id", "service(s)"),
        ):
            if n := await count(path, {param: ids}):
                effects.append(f"Deletes {n} {what}")
    elif otype.key == "dcim.interfaces":
        if n := await count("ipam/ip-addresses/", {"interface_id": ids}):
            effects.append(f"Deletes {n} IP address(es) assigned to it")
        if n := sum(1 for o in objs if o.get("cable")):
            effects.append(f"Leaves {n} cable(s) half-connected")
        if n := await count("dcim/interfaces/", {"lag_id": ids}):
            effects.append(f"{n} member interface(s) leave the LAG")
    elif otype.key == "ipam.prefixes":
        for p in objs:
            vrf = {"vrf_id": p["vrf"]["id"]} if p.get("vrf") else {"vrf_id": "null"}
            inner_ips = await count("ipam/ip-addresses/", {"parent": p["prefix"], **vrf})
            inner_prefixes = await count("ipam/prefixes/", {"within": p["prefix"], **vrf})
            if inner_ips or inner_prefixes:
                effects.append(
                    f"{p['prefix']}: {inner_ips} IP(s) and {inner_prefixes} child prefix(es) "
                    "inside it are kept"
                )
    elif otype.key == "ipam.vlans":
        if n := await count("dcim/interfaces/", {"vlan_id": ids}):
            effects.append(f"{n} interface(s) carrying it lose that VLAN assignment")
        if n := await count("ipam/prefixes/", {"vlan_id": ids}):
            effects.append(f"{n} prefix(es) lose their VLAN")
    elif otype.key == "ipam.ip-addresses":
        for field in ("primary_ip4_id", "primary_ip6_id"):
            hosts, _ = await nb.list("dcim/devices/", {field: ids, "brief": "true"}, limit=50)
            for h in hosts:
                effects.append(f"Clears the primary IP of {label(h)}")
    elif otype.key == "dcim.cables":
        for c in objs:
            effects.append(f"Disconnects {label(c)}: {_ends(c)}")
    return effects, blockers


def _ends(cable: dict[str, Any]) -> str:
    def side(terms: list[dict[str, Any]]) -> str:
        return ", ".join(
            f"{label((t.get('object') or {}).get('device'))} {(t.get('object') or {}).get('name')}"
            for t in terms
        )

    return f"{side(cable.get('a_terminations') or [])} <-> {side(cable.get('b_terminations') or [])}"


def register(mcp: FastMCP, settings: Settings) -> None:
    if not settings.netbox_mcp_enable_writes:
        return

    # --- generic -----------------------------------------------------------------------

    @mcp.tool(annotations=CREATE)
    async def netbox_create_object(
        ctx: Context,
        object_type: ObjectTypeArg,
        data: Annotated[
            dict[str, Any] | list[dict[str, Any]],
            Field(
                description="Field values for the new object, or a list of them for a bulk create. Related "
                "objects are referenced by id, e.g. {'name':'sw3','site':2,'role':4,'device_type':3}. "
                "See netbox_get_definition(object_type, section='write') for fields and required ones."
            ),
        ],
        confirm: ConfirmArg = None,
    ) -> dict[str, Any]:
        """Create one or more NetBox objects of any type (preview first). Field names are checked against
        the schema (NetBox would otherwise silently ignore misspelt fields)."""
        args = {"object_type": object_type, "data": data}
        nb, schema = get_netbox(ctx), await get_schema(ctx)
        otype = schema.resolve_type(object_type)
        items = data if isinstance(data, list) else [data]
        if not items or len(items) > MAX_BULK:
            raise InvalidRequest(f"Provide between 1 and {MAX_BULK} objects.")
        schema.check_write_fields(otype, {k for item in items for k in item})
        entries = [{"data": dict(i)} for i in items]
        await _labelled(nb, schema, content_type(otype), entries)
        plan = Plan(
            summary=f"Create {len(items)} {otype.key} object(s)",
            details={"object_type": otype.key, "objects": [e["data"] for e in entries]},
        )

        async def apply() -> dict[str, Any]:
            body: Any = [_normalise(i) for i in items] if isinstance(data, list) else _normalise(data)
            result = await nb.request("POST", otype.path, json=body)
            audit.info("caller=%s create %s ids=%s", current_caller(), otype.key, _ids(result))
            created = result if isinstance(result, list) else [result]
            return {"created": [_result(o) for o in created]}

        return await preview_or_apply(
            get_previews(ctx),
            tool="netbox_create_object",
            args=args,
            confirm=confirm,
            caller=current_caller(),
            plan=plan,
            apply=apply,
        )

    @mcp.tool(annotations=UPDATE)
    async def netbox_update_object(
        ctx: Context,
        object_type: ObjectTypeArg,
        data: Annotated[
            dict[str, Any],
            Field(description="Fields to change (partial update). Other fields are left as they are."),
        ],
        id: Annotated[int | None, Field(description="Object id to update.", ge=1)] = None,
        ids: Annotated[
            list[int] | None, Field(description="Apply the same change to several objects (bulk).")
        ] = None,
        confirm: ConfirmArg = None,
    ) -> dict[str, Any]:
        """Update (PATCH) one object, or apply the same change to many (preview first: shows each field's
        current -> new value, by name). Field names are checked against the schema."""
        args = {"object_type": object_type, "data": data, "id": id, "ids": ids}
        if (id is None) == (ids is None):
            raise InvalidRequest("Pass exactly one of id or ids.")
        if not data:
            raise InvalidRequest("data is empty: nothing to update.")
        nb, schema = get_netbox(ctx), await get_schema(ctx)
        otype = schema.resolve_type(object_type)
        schema.check_write_fields(otype, set(data))
        target = [id] if id is not None else sorted(set(ids or []))
        if not target or len(target) > MAX_BULK:
            raise InvalidRequest(f"Provide between 1 and {MAX_BULK} ids.")
        objs = await _existing(nb, otype, target)
        entries = []
        for o in sorted(objs, key=lambda o: o["id"]):
            changes, unchanged = field_changes(o, data)
            entries.append(
                {"object": f"{label(o)} (id {o['id']})", "changes": changes, "unchanged": unchanged}
            )
        await _labelled(nb, schema, content_type(otype), entries)
        details: dict[str, Any] = {
            "object_type": otype.key,
            "objects": [compact({k: e[k] for k in ("object", "changes", "unchanged")}) for e in entries],
        }
        if not any(e["changes"] for e in entries):
            details["note"] = "These values are already set: applying would change nothing."
        plan = Plan(
            summary=f"Update {len(objs)} {otype.key} object(s)", details=details, fingerprint=_state(objs)
        )

        async def apply() -> dict[str, Any]:
            changes = _normalise(data)
            if id is not None:
                result = await nb.request("PATCH", f"{otype.path}{id}/", json=changes)
            else:
                result = await nb.request("PATCH", otype.path, json=[{"id": i, **changes} for i in target])
            audit.info(
                "caller=%s update %s ids=%s fields=%s", current_caller(), otype.key, target, sorted(data)
            )
            updated = result if isinstance(result, list) else [result]
            return {"updated": [_result(o) for o in updated]}

        return await preview_or_apply(
            get_previews(ctx),
            tool="netbox_update_object",
            args=args,
            confirm=confirm,
            caller=current_caller(),
            plan=plan,
            apply=apply,
        )

    @mcp.tool(annotations=DELETE)
    async def netbox_delete_object(
        ctx: Context,
        object_type: ObjectTypeArg,
        ids: Annotated[list[int], Field(description="Ids of the objects to delete.", min_length=1)],
        confirm: ConfirmArg = None,
    ) -> dict[str, Any]:
        """Permanently delete NetBox objects by id (preview first: lists what goes and the side effects,
        e.g. a device's components and IPs, half-connected cables, or VMs that block the delete)."""
        args = {"object_type": object_type, "ids": ids}
        nb, schema = get_netbox(ctx), await get_schema(ctx)
        otype = schema.resolve_type(object_type)
        target = sorted(set(ids))
        if len(target) > MAX_BULK:
            raise InvalidRequest(f"At most {MAX_BULK} ids per call.")
        objs = await _existing(nb, otype, target)
        effects, blockers = await delete_effects(nb, otype, objs)
        plan = Plan(
            summary=f"Delete {len(objs)} {otype.key} object(s)",
            details=compact(
                {
                    "object_type": otype.key,
                    "objects": [f"{label(o)} (id {o['id']})" for o in objs],
                    "side_effects": effects,
                }
            ),
            fingerprint=_state(objs),
            blockers=blockers,
        )

        async def apply() -> dict[str, Any]:
            await nb.request("DELETE", otype.path, json=[{"id": i} for i in target])
            audit.info("caller=%s delete %s ids=%s", current_caller(), otype.key, target)
            return {"deleted": [f"{label(o)} (id {o['id']})" for o in objs]}

        return await preview_or_apply(
            get_previews(ctx),
            tool="netbox_delete_object",
            args=args,
            confirm=confirm,
            caller=current_caller(),
            plan=plan,
            apply=apply,
        )

    # --- IPAM allocation ---------------------------------------------------------------

    @mcp.tool(annotations=CREATE)
    async def netbox_allocate_ips(
        ctx: Context,
        prefix: Annotated[
            str | int | None, Field(description="Prefix (CIDR or id) to allocate from. Use this or ip_range.")
        ] = None,
        ip_range: Annotated[
            str | int | None,
            Field(description="IP range to allocate from: id, any address inside it, or 'start-end'."),
        ] = None,
        count: Annotated[int, Field(ge=1, le=100)] = 1,
        vrf: Annotated[
            str | int | None, Field(description="VRF name/RD/id if the prefix exists in several.")
        ] = None,
        description: str | None = None,
        dns_name: Annotated[str | None, Field(description="DNS name (only when allocating one IP).")] = None,
        status: Annotated[str, Field(description="active, reserved, dhcp, ...")] = "active",
        device: Annotated[
            str | None,
            Field(description="Assign to this device/VM's interface (needs interface; one IP only)."),
        ] = None,
        interface: Annotated[
            str | None, Field(description="Interface name on `device` to assign the IP to.")
        ] = None,
        set_primary: Annotated[
            bool,
            Field(description="Also make it the device/VM's primary IPv4/IPv6 (needs device+interface)."),
        ] = False,
        tags: TagsArg = None,
        confirm: ConfirmArg = None,
    ) -> dict[str, Any]:
        """Reserve the next free IP(s) in a prefix or IP range (preview first: shows the exact addresses).
        Optionally assign the IP to an interface and set it as the host's primary IP."""
        args = {k: v for k, v in locals().items() if k not in ("ctx", "confirm")}
        if (device is None) != (interface is None):
            raise InvalidRequest("Pass both device and interface to assign the IP, or neither.")
        if device and count != 1:
            raise InvalidRequest("Interface assignment only works when allocating a single IP.")
        if set_primary and not device:
            raise InvalidRequest("set_primary needs device and interface.")
        if dns_name and count != 1:
            raise InvalidRequest("dns_name only works when allocating a single IP.")
        nb = get_netbox(ctx)
        pool_path, pool_kind, pool = await resolve_ip_pool(nb, prefix, ip_range, vrf)
        free = await nb.get(f"{pool_path}available-ips/", {"limit": count})
        addresses = [f["address"] for f in free[:count]]

        item: dict[str, Any] = {"status": status}
        if description:
            item["description"] = description
        if dns_name:
            item["dns_name"] = dns_name
        if tags:
            item["tags"] = tags
        host_kind, host, iface = None, None, None
        details: dict[str, Any] = {pool_kind: label(pool), "addresses": addresses, "status": status}
        if device and interface:
            host_kind, iface = await resolve_interface(nb, host=device, interface=interface)
            item["assigned_object_type"] = (
                "dcim.interface" if host_kind == "device" else "virtualization.vminterface"
            )
            item["assigned_object_id"] = iface["id"]
            details["assign_to"] = f"{device} {iface['name']}"
            host_ref = iface.get("device") or iface.get("virtual_machine") or {}
            host_path = "dcim/devices/" if host_kind == "device" else "virtualization/virtual-machines/"
            host = await nb.get_object(f"{host_path}{host_ref['id']}/")
            if set_primary and addresses:
                field = "primary_ip4" if ipaddress.ip_interface(addresses[0]).version == 4 else "primary_ip6"
                details["set_primary"] = {"field": field, "from": label(host.get(field)), "to": addresses[0]}
        blockers = (
            [] if len(addresses) == count else [f"Only {len(addresses)} free address(es) in {label(pool)}"]
        )
        plan = Plan(
            summary=f"Allocate {count} IP address(es) from {label(pool)}",
            details=compact(details),
            fingerprint=[addresses, _state([iface] if iface else []), _state([host] if host else [])],
            blockers=blockers,
        )

        async def apply() -> dict[str, Any]:
            created = await nb.request(
                "POST", f"{pool_path}available-ips/", json=[_normalise(item) for _ in range(count)]
            )
            created = created if isinstance(created, list) else [created]
            audit.info(
                "caller=%s allocate ip %s=%s ids=%s", current_caller(), pool_kind, label(pool), _ids(created)
            )
            out: dict[str, Any] = {
                "allocated": [
                    compact({k: ip.get(k) for k in ("id", "address", "status", "dns_name")}) for ip in created
                ]
            }
            got = [ip["address"] for ip in created]
            if got != addresses:
                out["note"] = (
                    f"NetBox allocated {got} instead of the previewed {addresses} (concurrent change)."
                )
            if set_primary and host is not None:
                field = (
                    "primary_ip4"
                    if ipaddress.ip_interface(created[0]["address"]).version == 4
                    else "primary_ip6"
                )
                path = "dcim/devices/" if host_kind == "device" else "virtualization/virtual-machines/"
                await nb.request("PATCH", f"{path}{host['id']}/", json={field: created[0]["id"]})
                audit.info("caller=%s set %s %s=%s", current_caller(), host_kind, field, created[0]["id"])
                out["primary_set"] = f"{field} of {device}"
            return out

        return await preview_or_apply(
            get_previews(ctx),
            tool="netbox_allocate_ips",
            args=args,
            confirm=confirm,
            caller=current_caller(),
            plan=plan,
            apply=apply,
        )

    @mcp.tool(annotations=CREATE)
    async def netbox_allocate_prefix(
        ctx: Context,
        parent: Annotated[str | int, Field(description="Parent prefix (CIDR or id) to carve from.")],
        prefix_length: Annotated[
            int, Field(description="Size of the new child prefix, e.g. 24.", ge=1, le=128)
        ],
        count: Annotated[int, Field(ge=1, le=50)] = 1,
        vrf: Annotated[
            str | int | None, Field(description="VRF name/RD/id if the parent exists in several.")
        ] = None,
        description: str | None = None,
        status: Annotated[str, Field(description="active, reserved, container, ...")] = "active",
        vlan: Annotated[int | None, Field(description="VLAN id (object id, not VID) to attach.")] = None,
        tags: TagsArg = None,
        confirm: ConfirmArg = None,
    ) -> dict[str, Any]:
        """Carve the next free child prefix(es) of a given length out of a parent (preview first: shows
        the exact prefixes). The new prefixes inherit the parent's VRF."""
        args = {k: v for k, v in locals().items() if k not in ("ctx", "confirm")}
        nb = get_netbox(ctx)
        p = await resolve_prefix(nb, parent, vrf)
        parent_net = ipaddress.ip_network(p["prefix"])
        if not parent_net.prefixlen < prefix_length <= parent_net.max_prefixlen:
            raise InvalidRequest(f"/{prefix_length} cannot be carved from {parent_net}.")
        free = [
            ipaddress.ip_network(f["prefix"])
            for f in await nb.get(f"ipam/prefixes/{p['id']}/available-prefixes/")
        ]
        planned = next_child_prefixes(free, prefix_length, count)
        item: dict[str, Any] = {"prefix_length": prefix_length, "status": status}
        if description:
            item["description"] = description
        if vlan is not None:
            item["vlan"] = vlan
        if tags:
            item["tags"] = tags
        plan = Plan(
            summary=f"Allocate {count} /{prefix_length} prefix(es) from {p['prefix']}",
            details=compact(
                {
                    "parent": p["prefix"],
                    "vrf": label(p.get("vrf")) or "global",
                    "prefixes": planned,
                    "status": status,
                }
            ),
            fingerprint=planned,
            blockers=[]
            if len(planned) == count
            else [f"Only {len(planned)} free /{prefix_length} block(s) in {p['prefix']}"],
        )

        async def apply() -> dict[str, Any]:
            created = await nb.request(
                "POST",
                f"ipam/prefixes/{p['id']}/available-prefixes/",
                json=[_normalise(item) for _ in range(count)],
            )
            created = created if isinstance(created, list) else [created]
            audit.info(
                "caller=%s allocate prefix parent=%s ids=%s", current_caller(), p["prefix"], _ids(created)
            )
            return {
                "allocated": [compact({k: c.get(k) for k in ("id", "prefix", "status")}) for c in created]
            }

        return await preview_or_apply(
            get_previews(ctx),
            tool="netbox_allocate_prefix",
            args=args,
            confirm=confirm,
            caller=current_caller(),
            plan=plan,
            apply=apply,
        )

    @mcp.tool(annotations=CREATE)
    async def netbox_allocate_vlans(
        ctx: Context,
        group: Annotated[str | int, Field(description="VLAN group name, slug or id.")],
        names: Annotated[
            list[str],
            Field(description="One VLAN is created per name, e.g. ['GUEST'].", min_length=1, max_length=50),
        ],
        status: Annotated[str, Field(description="active, reserved or deprecated.")] = "active",
        description: str | None = None,
        tags: TagsArg = None,
        confirm: ConfirmArg = None,
    ) -> dict[str, Any]:
        """Create VLANs with the next free VIDs in a VLAN group (preview first: shows which VID each name
        gets). NetBox needs a name for each VLAN, so pass one name per VLAN wanted."""
        args = {k: v for k, v in locals().items() if k not in ("ctx", "confirm")}
        nb = get_netbox(ctx)
        g = await resolve_object(nb, (await get_schema(ctx)).resolve_type("ipam.vlan-groups"), group)
        free = await nb.get(f"ipam/vlan-groups/{g['id']}/available-vlans/", {"limit": len(names)})
        vids = [v["vid"] for v in free[: len(names)]]
        plan = Plan(
            summary=f"Create {len(names)} VLAN(s) in group {label(g)}",
            details={
                "group": label(g),
                "vlans": [f"{vid} {name}" for vid, name in zip(vids, names, strict=False)],
            },
            fingerprint=vids,
            blockers=[] if len(vids) == len(names) else [f"Only {len(vids)} free VID(s) in {label(g)}"],
        )

        async def apply() -> dict[str, Any]:
            items = []
            for name in names:
                vlan_item: dict[str, Any] = {"name": name, "status": status}
                if description:
                    vlan_item["description"] = description
                if tags:
                    vlan_item["tags"] = tags
                items.append(_normalise(vlan_item))
            created = await nb.request("POST", f"ipam/vlan-groups/{g['id']}/available-vlans/", json=items)
            created = created if isinstance(created, list) else [created]
            audit.info("caller=%s allocate vlan group=%s ids=%s", current_caller(), label(g), _ids(created))
            return {"allocated": [{"id": v["id"], "vid": v["vid"], "name": v["name"]} for v in created]}

        return await preview_or_apply(
            get_previews(ctx),
            tool="netbox_allocate_vlans",
            args=args,
            confirm=confirm,
            caller=current_caller(),
            plan=plan,
            apply=apply,
        )

    @mcp.tool(annotations=CREATE)
    async def netbox_allocate_asns(
        ctx: Context,
        asn_range: Annotated[str | int, Field(description="ASN range name, slug or id.")],
        count: Annotated[int, Field(ge=1, le=50)] = 1,
        description: str | None = None,
        tags: TagsArg = None,
        confirm: ConfirmArg = None,
    ) -> dict[str, Any]:
        """Reserve the next free AS number(s) from an ASN range (preview first: shows the exact ASNs)."""
        args = {k: v for k, v in locals().items() if k not in ("ctx", "confirm")}
        nb = get_netbox(ctx)
        r = await resolve_object(nb, (await get_schema(ctx)).resolve_type("ipam.asn-ranges"), asn_range)
        free = await nb.get(f"ipam/asn-ranges/{r['id']}/available-asns/", {"limit": count})
        asns = [a["asn"] for a in free[:count]]
        plan = Plan(
            summary=f"Allocate {count} ASN(s) from {label(r)}",
            details={"asn_range": label(r), "asns": asns},
            fingerprint=asns,
            blockers=[] if len(asns) == count else [f"Only {len(asns)} free ASN(s) in {label(r)}"],
        )

        async def apply() -> dict[str, Any]:
            item: dict[str, Any] = {}
            if description:
                item["description"] = description
            if tags:
                item["tags"] = tags
            created = await nb.request(
                "POST",
                f"ipam/asn-ranges/{r['id']}/available-asns/",
                json=[_normalise(item) for _ in range(count)],
            )
            created = created if isinstance(created, list) else [created]
            audit.info("caller=%s allocate asn range=%s ids=%s", current_caller(), label(r), _ids(created))
            return {"allocated": [{"id": a["id"], "asn": a["asn"]} for a in created]}

        return await preview_or_apply(
            get_previews(ctx),
            tool="netbox_allocate_asns",
            args=args,
            confirm=confirm,
            caller=current_caller(),
            plan=plan,
            apply=apply,
        )

    @mcp.tool(annotations=CREATE)
    async def netbox_connect_cable(
        ctx: Context,
        a_device: Annotated[
            str | None, Field(description="A-end device name/id (for a circuit termination: the circuit ID).")
        ] = None,
        a_port: Annotated[
            str | None,
            Field(description="A-end port name, e.g. Gi1/0/5 (for a circuit termination: A or Z)."),
        ] = None,
        b_device: Annotated[str | None, Field(description="B-end device name/id (or circuit ID).")] = None,
        b_port: Annotated[
            str | None, Field(description="B-end port name (or A/Z for a circuit termination).")
        ] = None,
        a_type: PortType = "interface",
        b_type: PortType = "interface",
        a_port_id: Annotated[
            int | None, Field(description="A-end port id, instead of device + port.")
        ] = None,
        b_port_id: Annotated[
            int | None, Field(description="B-end port id, instead of device + port.")
        ] = None,
        cable_label: Annotated[str | None, Field(description="Cable label, e.g. 'MCP-010'.")] = None,
        cable_type: Annotated[
            str | None, Field(description="e.g. cat6, cat6a, smf, mmf-om4, dac-passive.")
        ] = None,
        status: Annotated[str, Field(description="connected, planned or decommissioning.")] = "connected",
        color: Annotated[str | None, Field(description="Hex colour without '#', e.g. 'ff9800'.")] = None,
        length: Annotated[float | None, Field(gt=0)] = None,
        length_unit: Annotated[
            str | None, Field(description="m, cm, km, ft, in or mi (needed with length).")
        ] = None,
        description: str | None = None,
        tags: TagsArg = None,
        confirm: ConfirmArg = None,
    ) -> dict[str, Any]:
        """Connect two ports with a new cable, by name (preview first), e.g. lab-sw1 Gi1/0/5 -> lab-pp1
        FP2 (b_type 'front-port'). Port types: interface, front-port, rear-port, console-port,
        console-server-port, power-port, power-outlet, circuit-termination. Refuses ports that are
        already cabled or marked connected. After applying, reports where the A end's path now leads."""
        args = {k: v for k, v in locals().items() if k not in ("ctx", "confirm")}
        if length is not None and not length_unit:
            raise InvalidRequest("length needs length_unit (e.g. 'm').")
        nb = get_netbox(ctx)

        async def end(kind: str, device: str | None, port: str | None, port_id: int | None) -> dict[str, Any]:
            if kind == "circuit-termination":
                return await resolve_port(nb, kind, None, None, port_id, device, (port or "A").upper())
            return await resolve_port(nb, kind, device, port, port_id, None, "A")

        a = await end(a_type, a_device, a_port, a_port_id)
        b = await end(b_type, b_device, b_port, b_port_id)
        a_ct, b_ct = PORT_CONTENT_TYPES[a_type], PORT_CONTENT_TYPES[b_type]
        if (a_ct, a["id"]) == (b_ct, b["id"]):
            raise InvalidRequest("Both ends are the same port.")
        blockers = []
        for side, port, kind in (("A", a, a_type), ("B", b, b_type)):
            if port.get("cable"):
                peers = terms_label(port.get("link_peers")) or "?"
                blockers.append(
                    f"{side} end {_port_label(port, kind)} already has cable "
                    f"{label(port['cable'])} (to {peers})"
                )
            if port.get("mark_connected"):
                blockers.append(f"{side} end {_port_label(port, kind)} is marked as connected")

        body: dict[str, Any] = {
            "a_terminations": [{"object_type": a_ct, "object_id": a["id"]}],
            "b_terminations": [{"object_type": b_ct, "object_id": b["id"]}],
            "status": status,
        }
        for key, value in (
            ("label", cable_label),
            ("type", cable_type),
            ("color", color),
            ("length", length),
            ("length_unit", length_unit),
            ("description", description),
            ("tags", tags),
        ):
            if value is not None:
                body[key] = value
        a_label, b_label = _port_label(a, a_type), _port_label(b, b_type)
        plan = Plan(
            summary=f"Connect {a_label} <-> {b_label}",
            details=compact(
                {
                    "a": a_label,
                    "b": b_label,
                    "cable": {k: v for k, v in body.items() if k not in ("a_terminations", "b_terminations")},
                }
            ),
            fingerprint=_state([a, b]),
            blockers=blockers,
        )

        async def apply() -> dict[str, Any]:
            cable = await nb.request("POST", "dcim/cables/", json=_normalise(body))
            audit.info(
                "caller=%s connect cable id=%s %s <-> %s", current_caller(), cable["id"], a_label, b_label
            )
            out: dict[str, Any] = {
                "cable": compact(
                    {k: cable.get(k) for k in ("id", "label", "type", "status", "color", "length")}
                )
            }
            if a_type in TRACEABLE:
                segments = await nb.get(f"{port_path(a_type)}{a['id']}/trace/") or []
                if segments:
                    far = segments[-1][2] or []
                    out["path_far_end"] = terms_label(far)
                    out["path_complete"] = is_endpoint(far)
            return out

        return await preview_or_apply(
            get_previews(ctx),
            tool="netbox_connect_cable",
            args=args,
            confirm=confirm,
            caller=current_caller(),
            plan=plan,
            apply=apply,
        )


PORT_CONTENT_TYPES = {
    "interface": "dcim.interface",
    "front-port": "dcim.frontport",
    "rear-port": "dcim.rearport",
    "console-port": "dcim.consoleport",
    "console-server-port": "dcim.consoleserverport",
    "power-port": "dcim.powerport",
    "power-outlet": "dcim.poweroutlet",
    "power-feed": "dcim.powerfeed",
    "circuit-termination": "circuits.circuittermination",
}
TRACEABLE = {"interface", "console-port", "console-server-port", "power-port", "power-outlet", "power-feed"}


def _port_label(port: dict[str, Any], kind: str) -> str:
    if kind == "circuit-termination":
        return f"{label(port.get('circuit'))} term {port.get('term_side')} (circuit-termination)"
    return f"{label(port.get('device'))} {port.get('name')} ({kind})"
