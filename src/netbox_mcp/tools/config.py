"""Config rendering, config contexts and custom-field discovery."""

from __future__ import annotations

import asyncio
import difflib
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.deps import get_netbox, get_schema
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.formatting import MAX_RESPONSE_CHARS, compact
from netbox_mcp.tools.common import HostKind, choice, content_type, label, resolve_host

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

HostArg = Annotated[str | int, Field(description="Device or virtual machine name (or id).")]
KindArg = Annotated[
    Literal["device", "vm"] | None,
    Field(description="Force 'device' or 'vm'. Default: try devices, then VMs."),
]

# Config-context assignment criteria -> where their (possibly nested) objects live, for ancestor lookups.
_HIERARCHICAL = {
    "regions": "dcim/regions/",
    "site_groups": "dcim/site-groups/",
    "locations": "dcim/locations/",
    "roles": "dcim/device-roles/",
    "platforms": "dcim/platforms/",
    "tenant_groups": "tenancy/tenant-groups/",
    "cluster_groups": "virtualization/cluster-groups/",
}
_CRITERIA = (
    "regions",
    "site_groups",
    "sites",
    "locations",
    "device_types",
    "roles",
    "platforms",
    "cluster_types",
    "cluster_groups",
    "clusters",
    "tenant_groups",
    "tenants",
    "tags",
)

# Custom-field filter lookups NetBox honours, by field type (checked against NetBox 4.7). Anything
# else, e.g. cf_<text field>__ic, is silently ignored by NetBox and returns every object.
_CF_ALLOWED_LOOKUPS = {
    "integer": {"n", "gt", "gte", "lt", "lte"},
    "decimal": {"n", "gt", "gte", "lt", "lte"},
    "date": {"n", "gt", "gte", "lt", "lte"},
    "datetime": {"n", "gt", "gte", "lt", "lte"},
    "select": {"n"},
    "multiselect": {"n"},
    "object": {"n"},
    "multiobject": {"n"},
}


def cf_filter_hint(name: str, ftype: str, logic: str) -> str:
    if logic == "disabled":
        return "filtering disabled"
    if ftype in ("text", "longtext", "url"):
        if logic == "loose":
            return f"cf_{name}=part (matches anywhere, case-insensitive)"
        return f"cf_{name}=exact value"
    lookups = sorted(_CF_ALLOWED_LOOKUPS.get(ftype, set()))
    extra = "".join(f", cf_{name}__{lk}=..." for lk in lookups)
    return f"cf_{name}=value{extra}"


def _host_path(kind: HostKind) -> str:
    return "dcim/devices/" if kind == "device" else "virtualization/virtual-machines/"


async def _ancestors(
    nb: NetBoxClient, path: str, obj_id: int | None, cache: dict[tuple[str, int], int | None]
) -> set[int]:
    """An object's id plus all its parents' ids (regions, locations, ... are trees)."""
    ids: set[int] = set()
    while obj_id is not None and obj_id not in ids:
        ids.add(obj_id)
        key = (path, obj_id)
        if key not in cache:
            parent = (await nb.get_object(f"{path}{obj_id}/")).get("parent")
            cache[key] = parent["id"] if parent else None
        obj_id = cache[key]
    return ids


def _id(obj: Any) -> int | None:
    return obj.get("id") if isinstance(obj, dict) else None


async def host_criteria(
    nb: NetBoxClient, kind: HostKind, host: dict[str, Any], needed: set[str]
) -> dict[str, set[Any]]:
    """The values a host offers for each config-context criterion (ids; tag slugs for tags)."""
    cache: dict[tuple[str, int], int | None] = {}
    values: dict[str, set[Any]] = {c: set() for c in _CRITERIA}
    site = await nb.get_object(f"dcim/sites/{host['site']['id']}/") if host.get("site") else {}
    cluster = (
        await nb.get_object(f"virtualization/clusters/{host['cluster']['id']}/")
        if host.get("cluster")
        else {}
    )
    tenant = await nb.get_object(f"tenancy/tenants/{host['tenant']['id']}/") if host.get("tenant") else {}
    direct: dict[str, int | None] = {
        "regions": _id(site.get("region")),
        "site_groups": _id(site.get("group")),
        "sites": _id(host.get("site")),
        "locations": _id(host.get("location")),
        "device_types": _id(host.get("device_type")) if kind == "device" else None,
        "roles": _id(host.get("role")),
        "platforms": _id(host.get("platform")),
        "cluster_types": _id(cluster.get("type")),
        "cluster_groups": _id(cluster.get("group")),
        "clusters": _id(host.get("cluster")),
        "tenant_groups": _id(tenant.get("group")),
        "tenants": _id(host.get("tenant")),
    }
    for crit, obj_id in direct.items():
        if obj_id is None or crit not in needed:
            continue
        if crit in _HIERARCHICAL:
            values[crit] = await _ancestors(nb, _HIERARCHICAL[crit], obj_id, cache)
        else:
            values[crit] = {obj_id}
    values["tags"] = {t.get("slug") for t in host.get("tags") or []}
    return values


def context_matches(ctx: dict[str, Any], host_values: dict[str, set[Any]]) -> list[str] | None:
    """Criteria a config context matched on ([] = applies everywhere), or None if it doesn't apply.
    Each non-empty criterion must match (AND); within a criterion any listed object matches (OR)."""
    matched = []
    for crit in _CRITERIA:
        wanted = ctx.get(crit) or []
        if not wanted:
            continue
        ids = {w.get("slug") if crit == "tags" else w.get("id") for w in wanted}
        if not ids & host_values.get(crit, set()):
            return None
        matched.append(crit)
    return matched


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def netbox_render_config(ctx: Context, name: HostArg, kind: KindArg = None) -> dict[str, Any]:
        """Render a device's or VM's intended configuration from its config template (assigned to the
        device, its role or its platform), with its config context as template variables."""
        nb = get_netbox(ctx)
        hk, host = await resolve_host(nb, name, kind)
        rendered = await nb.request("POST", f"{_host_path(hk)}{host['id']}/render-config/", json={})
        content = str(rendered.get("content") or "")
        out: dict[str, Any] = {
            "kind": hk,
            "name": label(host),
            "template": label(rendered.get("configtemplate")),
            "lines": content.count("\n") + 1 if content else 0,
            "config": content[:MAX_RESPONSE_CHARS],
        }
        if len(content) > MAX_RESPONSE_CHARS:
            out["note"] = f"Config truncated to {MAX_RESPONSE_CHARS} of {len(content)} characters."
        return out

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_config_context(ctx: Context, name: HostArg, kind: KindArg = None) -> dict[str, Any]:
        """A device's or VM's merged config context, its local context data, and which config contexts
        contributed (in merge order: lower weight first, later ones override; local data wins last)."""
        nb = get_netbox(ctx)
        hk, ref = await resolve_host(nb, name, kind)
        host, (contexts, _) = await asyncio.gather(
            nb.get_object(f"{_host_path(hk)}{ref['id']}/"),
            nb.list("extras/config-contexts/", {"is_active": "true"}, limit=1000),
        )
        needed = {c for c in _CRITERIA if any(cx.get(c) for cx in contexts)}
        values = await host_criteria(nb, hk, host, needed)
        sources = []
        for cx in sorted(contexts, key=lambda c: (c.get("weight", 1000), c.get("name", ""))):
            matched = context_matches(cx, values)
            if matched is None:
                continue
            sources.append(
                {
                    "name": cx.get("name"),
                    "weight": cx.get("weight"),
                    "matched_on": matched or ["(everything)"],
                    "provides": sorted((cx.get("data") or {}).keys()),
                }
            )
        out: dict[str, Any] = {
            "kind": hk,
            "name": label(host),
            "config_context": host.get("config_context") or {},
            "sources": sources,
        }
        if host.get("local_context_data"):
            out["local_context_data"] = host["local_context_data"]
        return out

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_custom_fields(
        ctx: Context,
        object_type: Annotated[
            str | None,
            Field(
                description="Only fields on this type, e.g. 'devices' or 'virtualization.virtual-machines'."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """The custom fields defined in NetBox: name, type, which object types use them, choices, and
        how to filter on them with netbox_list_objects (filters named cf_<name>)."""
        nb = get_netbox(ctx)
        params: dict[str, Any] = {}
        if object_type is not None:
            params["object_type"] = content_type((await get_schema(ctx)).resolve_type(object_type))
        fields, _ = await nb.list("extras/custom-fields/", params, limit=500)

        async def choices(cf: dict[str, Any]) -> list[str] | None:
            if not cf.get("choice_set"):
                return None
            items, _ = await nb.list(
                f"extras/custom-field-choice-sets/{cf['choice_set']['id']}/choices/", {}, limit=200
            )
            return [str(c.get("id")) for c in items]

        all_choices = await asyncio.gather(*(choices(cf) for cf in fields))
        out = []
        for cf, opts in zip(fields, all_choices, strict=True):
            ftype = choice(cf.get("type"))
            entry = {
                "name": cf["name"],
                "label": cf.get("label") or None,
                "type": ftype,
                "object_types": cf.get("object_types"),
                "required": cf.get("required") or None,
                "default": cf.get("default"),
                "choices": opts,
                "related_object_type": cf.get("related_object_type"),
                "description": cf.get("description") or None,
            }
            entry["filter"] = cf_filter_hint(cf["name"], ftype, choice(cf.get("filter_logic")))
            out.append(compact(entry))
        return {"count": len(out), "custom_fields": out}


async def check_custom_field_filters(nb: NetBoxClient, ct: str, names: list[str]) -> None:
    """Validate cf_<name>[__lookup] filters against the custom fields defined for a content type.
    NetBox silently ignores unknown fields and unsupported lookups, which would return everything."""
    fields, _ = await nb.list("extras/custom-fields/", {"object_type": ct}, limit=500)
    by_name = {f["name"]: f for f in fields}
    problems = []
    for raw in names:
        name, _, lookup = raw[len("cf_") :].partition("__")
        cf = by_name.get(name)
        if cf is None:
            close = difflib.get_close_matches(name, list(by_name), n=3, cutoff=0.6)
            problems.append(
                f"'{raw}': no such custom field"
                + (f" (did you mean {', '.join('cf_' + c for c in close)}?)" if close else "")
            )
            continue
        ftype, logic = choice(cf.get("type")), choice(cf.get("filter_logic"))
        if logic == "disabled":
            problems.append(f"'{raw}': filtering is disabled for this custom field")
        elif lookup and lookup not in _CF_ALLOWED_LOOKUPS.get(ftype, set()):
            hint = cf_filter_hint(name, ftype, logic)
            problems.append(f"'{raw}': NetBox ignores __{lookup} on a {ftype} field. Use {hint}")
    if problems:
        available = ", ".join(sorted(by_name)) or "none"
        raise InvalidRequest(
            f"Invalid custom field filter(s) for {ct}: {'; '.join(problems)}. "
            f"Custom fields here: {available}."
        )
