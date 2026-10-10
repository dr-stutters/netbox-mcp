"""Virtualization clusters, services, tenants and contacts."""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.deps import get_netbox, get_schema
from netbox_mcp.formatting import compact, compact_dict
from netbox_mcp.tools.common import choice, content_type, label, resolve_host, resolve_object

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

LIST_SAMPLE = 50  # names listed per category before summarising

# Tenant-owned object types shown in a tenant summary: (label, API path, Tenant count field).
TENANT_OBJECTS = (
    ("sites", "dcim/sites/", "site_count"),
    ("racks", "dcim/racks/", "rack_count"),
    ("devices", "dcim/devices/", "device_count"),
    ("virtual_machines", "virtualization/virtual-machines/", "virtualmachine_count"),
    ("clusters", "virtualization/clusters/", "cluster_count"),
    ("vrfs", "ipam/vrfs/", "vrf_count"),
    ("prefixes", "ipam/prefixes/", "prefix_count"),
    ("vlans", "ipam/vlans/", "vlan_count"),
    ("ip_addresses", "ipam/ip-addresses/", "ipaddress_count"),
    ("circuits", "circuits/circuits/", "circuit_count"),
)


def _service_row(s: dict[str, Any]) -> dict[str, Any]:
    parent_type = (s.get("parent_object_type") or "").split(".")[-1]
    kind = {"virtualmachine": "vm", "fhrpgroup": "fhrp-group"}.get(parent_type, parent_type)
    return compact_dict(
        {
            "name": s.get("name"),
            "protocol": choice(s.get("protocol")),
            "ports": s.get("port_mappings") or s.get("ports"),
            "host": f"{label(s.get('parent'))} ({kind})" if s.get("parent") else None,
            "ip_addresses": [label(ip) for ip in s.get("ipaddresses") or []],
            "description": s.get("description"),
        }
    )


async def _contacts(
    nb: NetBoxClient, assignments: list[dict[str, Any]], via: str | None
) -> list[dict[str, Any]]:
    """Expand contact assignments with the contact's details (email, phone, title)."""
    ids = sorted({a["contact"]["id"] for a in assignments if a.get("contact")})
    details = {}
    if ids:
        contacts, _ = await nb.list("tenancy/contacts/", {"id": ids}, limit=len(ids))
        details = {c["id"]: c for c in contacts}
    rows = []
    for a in assignments:
        c = details.get((a.get("contact") or {}).get("id"), {})
        rows.append(
            compact_dict(
                {
                    "name": label(a.get("contact")),
                    "role": label(a.get("role")),
                    "priority": choice(a.get("priority")),
                    "title": c.get("title"),
                    "email": c.get("email"),
                    "phone": c.get("phone"),
                    "via": via,
                }
            )
        )
    order = {"primary": 0, "secondary": 1, "tertiary": 2, "inactive": 3}
    return sorted(rows, key=lambda r: order.get(r.get("priority", ""), 9))


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def netbox_cluster_summary(
        ctx: Context,
        cluster: Annotated[
            str | int | None, Field(description="Cluster name or id. Omit to list all clusters.")
        ] = None,
    ) -> dict[str, Any]:
        """A virtualization cluster: type, group, scope, host devices, VMs by status and per host, and
        allocated vCPU / memory / disk (memory and disk in MB, as NetBox stores them).
        Without a name, lists all clusters with their counts."""
        nb = get_netbox(ctx)
        if cluster is None:
            items, total = await nb.list("virtualization/clusters/", {}, limit=200)
            return {
                "count": total,
                "clusters": [
                    compact(
                        {
                            "name": c.get("name"),
                            "type": label(c.get("type")),
                            "status": choice(c.get("status")),
                            "scope": label(c.get("scope")),
                            "hosts": c.get("device_count"),
                            "vms": c.get("virtualmachine_count"),
                        }
                    )
                    for c in items
                ],
            }
        c = await resolve_object(nb, (await get_schema(ctx)).resolve_type("virtualization.clusters"), cluster)
        (hosts, _), (vms, vm_total) = await asyncio.gather(
            nb.list("dcim/devices/", {"cluster_id": c["id"], "fields": "id,name,status,role"}, limit=500),
            nb.list(
                "virtualization/virtual-machines/",
                {"cluster_id": c["id"], "fields": "id,name,status,device,vcpus,memory,disk"},
                limit=5000,
            ),
        )
        per_host = Counter(label(v.get("device")) or "(no host)" for v in vms)
        out: dict[str, Any] = {
            "name": c.get("name"),
            "type": label(c.get("type")),
            "group": label(c.get("group")),
            "status": choice(c.get("status")),
            "scope": label(c.get("scope")),
            "tenant": label(c.get("tenant")),
            "hosts": [f"{label(h)} ({choice(h.get('status'))})" for h in hosts],
            "vm_count": vm_total,
            "vms_by_status": dict(Counter(choice(v.get("status")) for v in vms).most_common()),
            "vms_per_host": dict(per_host.most_common()),
            "allocated": compact(
                {
                    "vcpus": c.get("allocated_vcpus"),
                    "memory_mb": c.get("allocated_memory"),
                    "disk_mb": c.get("allocated_disk"),
                }
            ),
            "vms": [
                compact(
                    {
                        "name": v.get("name"),
                        "status": choice(v.get("status")),
                        "host": label(v.get("device")),
                        "vcpus": v.get("vcpus"),
                        "memory_mb": v.get("memory"),
                        "disk_mb": v.get("disk"),
                    }
                )
                for v in sorted(vms, key=lambda v: v.get("name") or "")[:LIST_SAMPLE]
            ],
        }
        if vm_total > LIST_SAMPLE:
            out["note"] = (
                f"Listing {LIST_SAMPLE} of {vm_total} VMs; use netbox_list_objects with cluster_id={c['id']}."
            )
        return compact_dict(out)

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_service_lookup(
        ctx: Context,
        port: Annotated[int | None, Field(description="Port number, e.g. 443.", ge=1, le=65535)] = None,
        name: Annotated[str | None, Field(description="Service name (partial match), e.g. 'ssh'.")] = None,
        host: Annotated[
            str | int | None, Field(description="Device or VM name/id running the service.")
        ] = None,
        protocol: Literal["tcp", "udp", "sctp"] | None = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> dict[str, Any]:
        """Services (listening applications) recorded in NetBox: 'what listens on port 443?' or 'what
        runs on host X?'. Returns each service's protocol, ports, host (device/VM) and IPs."""
        nb = get_netbox(ctx)
        params: dict[str, Any] = {}
        if port is not None:
            params["port"] = port
        if name:
            params["name__ic"] = name
        if protocol:
            params["protocol"] = protocol
        if host is not None:
            kind, h = await resolve_host(nb, host)
            params["device_id" if kind == "device" else "virtual_machine_id"] = h["id"]
        services, total = await nb.list("ipam/services/", params, limit=limit)
        out: dict[str, Any] = {"count": total, "services": [_service_row(s) for s in services]}
        if total > len(services):
            out["note"] = (
                f"Showing {len(services)} of {total}; narrow with port/name/host/protocol or raise limit."
            )
        return out

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_tenant_summary(
        ctx: Context, tenant: Annotated[str | int, Field(description="Tenant name, slug or id.")]
    ) -> dict[str, Any]:
        """Everything a tenant owns: counts per object type (sites, racks, devices, VMs, clusters, VRFs,
        prefixes, VLANs, IPs, circuits) with example names, plus the tenant's contacts."""
        nb = get_netbox(ctx)
        t = await resolve_object(nb, (await get_schema(ctx)).resolve_type("tenancy.tenants"), tenant)
        owned = [(key, path) for key, path, count_field in TENANT_OBJECTS if t.get(count_field)]
        samples = await asyncio.gather(
            *(nb.list(path, {"tenant_id": t["id"], "brief": "true"}, limit=10) for _, path in owned)
        )
        assignments, _ = await nb.list(
            "tenancy/contact-assignments/", {"object_type": "tenancy.tenant", "object_id": t["id"]}, limit=100
        )
        return compact_dict(
            {
                "name": t.get("name"),
                "slug": t.get("slug"),
                "group": label(t.get("group")),
                "description": t.get("description"),
                "counts": {key: t.get(field) for key, _, field in TENANT_OBJECTS if t.get(field)},
                "examples": {
                    key: [label(o) for o in items]
                    for (key, _), (items, _) in zip(owned, samples, strict=True)
                },
                "contacts": await _contacts(nb, assignments, None),
            }
        )

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_contacts_for(
        ctx: Context,
        object_type: Annotated[str, Field(description="Object type, e.g. 'devices', 'sites', 'circuits'.")],
        object: Annotated[str | int, Field(description="The object's name / cid / id.")],
        include_inherited: Annotated[
            bool, Field(description="Also include contacts of the object's site and tenant.")
        ] = True,
    ) -> dict[str, Any]:
        """Who to contact about an object: assigned contacts with role, priority, email and phone,
        primary first. Optionally includes contacts inherited from its site and tenant."""
        nb = get_netbox(ctx)
        schema = await get_schema(ctx)
        otype = schema.resolve_type(object_type)
        obj = await resolve_object(nb, otype, object)
        ct = content_type(otype)
        scopes: list[tuple[str, int, str | None]] = [(ct, obj["id"], None)]
        if include_inherited:
            for field, scope_ct in (("site", "dcim.site"), ("tenant", "tenancy.tenant")):
                if isinstance(obj.get(field), dict) and (ct, obj["id"]) != (scope_ct, obj[field]["id"]):
                    scopes.append((scope_ct, obj[field]["id"], f"{field} {label(obj[field])}"))
        found = await asyncio.gather(
            *(
                nb.list("tenancy/contact-assignments/", {"object_type": sct, "object_id": sid}, limit=100)
                for sct, sid, _ in scopes
            )
        )
        rows: list[dict[str, Any]] = []
        for (_, _, via), (assignments, _) in zip(scopes, found, strict=True):
            rows.extend(await _contacts(nb, assignments, via))
        out: dict[str, Any] = {"object": f"{ct} {label(obj)}", "contacts": rows}
        if not rows:
            out["note"] = "No contacts assigned" + (
                " (including its site and tenant)." if include_inherited else "."
            )
        return out
