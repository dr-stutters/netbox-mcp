"""Write tools: generic create/update/delete and IPAM allocation.

Only registered when NETBOX_MCP_ENABLE_WRITES=true, so read-only servers never expose them."""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.deps import current_caller, get_netbox, get_schema
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.formatting import compact, list_result, object_result
from netbox_mcp.schema import ObjectType
from netbox_mcp.tools.common import label, resolve_interface, resolve_prefix
from netbox_mcp.tools.generic import ObjectTypeArg

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
    """Fetch brief objects for ids, failing if any are missing (so nothing is half-applied)."""
    found, _ = await nb.list(otype.path, {"id": ids, "brief": "true"}, limit=len(ids))
    missing = sorted(set(ids) - {o["id"] for o in found})
    if missing:
        raise InvalidRequest(f"No {otype.key} with id(s) {missing}; nothing was changed.")
    return found


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
    ) -> ToolResult:
        """Create one or more NetBox objects of any type. Field names are checked against the schema
        before sending (NetBox would otherwise silently ignore misspelt fields)."""
        schema = await get_schema(ctx)
        otype = schema.resolve_type(object_type)
        items = data if isinstance(data, list) else [data]
        if not items or len(items) > MAX_BULK:
            raise InvalidRequest(f"Provide between 1 and {MAX_BULK} objects.")
        schema.check_write_fields(otype, {k for item in items for k in item})
        body: Any = [_normalise(i) for i in items] if isinstance(data, list) else _normalise(data)

        result = await get_netbox(ctx).request("POST", otype.path, json=body)
        audit.info("caller=%s create %s ids=%s", current_caller(), otype.key, _ids(result))
        if isinstance(result, list):
            return list_result(result, count=len(result), offset=0, extra={"created": len(result)})
        return object_result(result)

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
    ) -> ToolResult:
        """Update (PATCH) one object, or apply the same change to many. Field names are checked
        against the schema, and all ids must exist before anything is changed."""
        if (id is None) == (ids is None):
            raise InvalidRequest("Pass exactly one of id or ids.")
        if not data:
            raise InvalidRequest("data is empty: nothing to update.")
        schema = await get_schema(ctx)
        otype = schema.resolve_type(object_type)
        schema.check_write_fields(otype, set(data))
        nb = get_netbox(ctx)
        changes = _normalise(data)

        if id is not None:
            result = await nb.request("PATCH", f"{otype.path}{id}/", json=changes)
            audit.info(
                "caller=%s update %s ids=[%s] fields=%s", current_caller(), otype.key, id, sorted(data)
            )
            return object_result(result)

        target = sorted(set(ids or []))
        if not target or len(target) > MAX_BULK:
            raise InvalidRequest(f"Provide between 1 and {MAX_BULK} ids.")
        await _existing(nb, otype, target)
        result = await nb.request("PATCH", otype.path, json=[{"id": i, **changes} for i in target])
        audit.info("caller=%s update %s ids=%s fields=%s", current_caller(), otype.key, target, sorted(data))
        return list_result(result, count=len(result), offset=0, extra={"updated": len(result)})

    @mcp.tool(annotations=DELETE)
    async def netbox_delete_object(
        ctx: Context,
        object_type: ObjectTypeArg,
        ids: Annotated[list[int], Field(description="Ids of the objects to delete.", min_length=1)],
    ) -> dict[str, Any]:
        """Permanently delete NetBox objects by id. All ids must exist or nothing is deleted.
        Deleting a device also deletes its interfaces and other components."""
        schema = await get_schema(ctx)
        otype = schema.resolve_type(object_type)
        target = sorted(set(ids))
        if len(target) > MAX_BULK:
            raise InvalidRequest(f"At most {MAX_BULK} ids per call.")
        nb = get_netbox(ctx)
        found = await _existing(nb, otype, target)
        await nb.request("DELETE", otype.path, json=[{"id": i} for i in target])
        audit.info("caller=%s delete %s ids=%s", current_caller(), otype.key, target)
        return {"object_type": otype.key, "deleted": [{"id": o["id"], "display": label(o)} for o in found]}

    # --- IPAM allocation ---------------------------------------------------------------

    @mcp.tool(annotations=CREATE)
    async def netbox_allocate_ips(
        ctx: Context,
        prefix: Annotated[str | int, Field(description="Prefix (CIDR or id) to allocate from.")],
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
    ) -> dict[str, Any]:
        """Reserve the next free IP(s) in a prefix (atomic in NetBox, so safe against races).
        Optionally assign the IP to an interface and set it as the host's primary IP."""
        if (device is None) != (interface is None):
            raise InvalidRequest("Pass both device and interface to assign the IP, or neither.")
        if device and count != 1:
            raise InvalidRequest("Interface assignment only works when allocating a single IP.")
        if set_primary and not device:
            raise InvalidRequest("set_primary needs device and interface.")
        if dns_name and count != 1:
            raise InvalidRequest("dns_name only works when allocating a single IP.")
        nb = get_netbox(ctx)
        p = await resolve_prefix(nb, prefix, vrf)

        item: dict[str, Any] = {"status": status}
        if description:
            item["description"] = description
        if dns_name:
            item["dns_name"] = dns_name
        if tags:
            item["tags"] = tags
        host_kind, host_id = None, None
        if device and interface:
            host_kind, iface = await resolve_interface(nb, host=device, interface=interface)
            item["assigned_object_type"] = (
                "dcim.interface" if host_kind == "device" else "virtualization.vminterface"
            )
            item["assigned_object_id"] = iface["id"]
            host_id = (iface.get("device") or iface.get("virtual_machine") or {}).get("id")

        created = await nb.request(
            "POST", f"ipam/prefixes/{p['id']}/available-ips/", json=[_normalise(item) for _ in range(count)]
        )
        created = created if isinstance(created, list) else [created]
        audit.info("caller=%s allocate ip prefix=%s ids=%s", current_caller(), p["prefix"], _ids(created))

        out: dict[str, Any] = {
            "prefix": p["prefix"],
            "allocated": [
                compact({k: ip.get(k) for k in ("id", "address", "status", "dns_name", "vrf")})
                for ip in created
            ],
        }
        if set_primary and host_kind and host_id:
            ip = created[0]
            field = "primary_ip4" if ipaddress.ip_interface(ip["address"]).version == 4 else "primary_ip6"
            path = "dcim/devices/" if host_kind == "device" else "virtualization/virtual-machines/"
            await nb.request("PATCH", f"{path}{host_id}/", json={field: ip["id"]})
            audit.info("caller=%s set %s %s=%s", current_caller(), host_kind, field, ip["id"])
            out["primary_set"] = f"{field} of {device}"
        if device:
            out["assigned_to"] = f"{device} {interface}"
        return out

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
    ) -> dict[str, Any]:
        """Carve the next free child prefix(es) of a given length out of a parent (atomic in NetBox).
        The new prefixes inherit the parent's VRF."""
        nb = get_netbox(ctx)
        p = await resolve_prefix(nb, parent, vrf)
        parent_net = ipaddress.ip_network(p["prefix"])
        if not parent_net.prefixlen < prefix_length <= parent_net.max_prefixlen:
            raise InvalidRequest(f"/{prefix_length} cannot be carved from {parent_net}.")
        item: dict[str, Any] = {"prefix_length": prefix_length, "status": status}
        if description:
            item["description"] = description
        if vlan is not None:
            item["vlan"] = vlan
        if tags:
            item["tags"] = tags
        created = await nb.request(
            "POST",
            f"ipam/prefixes/{p['id']}/available-prefixes/",
            json=[_normalise(item) for _ in range(count)],
        )
        created = created if isinstance(created, list) else [created]
        audit.info("caller=%s allocate prefix parent=%s ids=%s", current_caller(), p["prefix"], _ids(created))
        return {
            "parent": p["prefix"],
            "allocated": [
                compact({k: c.get(k) for k in ("id", "prefix", "status", "vrf", "vlan")}) for c in created
            ],
        }
