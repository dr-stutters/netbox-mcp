"""Change history (NetBox changelog) and journal entries."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.deps import current_caller, get_netbox, get_previews, get_schema
from netbox_mcp.errors import InvalidRequest, NetBoxError
from netbox_mcp.formatting import compact
from netbox_mcp.preview import Plan, preview_or_apply
from netbox_mcp.schema import ObjectType, SchemaIndex
from netbox_mcp.tools.common import choice, content_type, label, resolve_object

audit = logging.getLogger("netbox_mcp.audit")

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
CREATE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)

# Bookkeeping fields that change on every save and say nothing about what the user changed.
_IGNORED_FIELDS = {"last_updated", "created"}
_DURATION_RE = re.compile(r"^(\d+)\s*([mhdw])$")
# Schema summaries for reference fields look like "IPAddress id (int), nullable".
_REF_RE = re.compile(r"^(\w+) id \(int\)")
_READ_REF_RE = re.compile(r"^(\w+)(?:,|$)")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}

ObjectTypeArg = Annotated[
    str | None,
    Field(description="Object type, e.g. 'dcim.devices', 'ipam.prefixes' or a short name like 'devices'."),
]
ObjectArg = Annotated[
    str | int | None,
    Field(description="The object: its id, or its name / address / prefix / label. Needs object_type."),
]


def parse_time(value: str | None, *, now: datetime | None = None) -> str | None:
    """'30m', '24h', '7d', '2w' (relative to now) or an ISO date/time -> ISO 8601 string."""
    if not value:
        return None
    now = now or datetime.now(UTC)
    m = _DURATION_RE.match(value.strip().lower())
    if m:
        return (now - timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})).isoformat()
    try:
        return datetime.fromisoformat(value.strip()).isoformat()
    except ValueError as e:
        raise InvalidRequest(f"Can't parse time {value!r}: use e.g. '24h', '7d' or '2026-10-01'.") from e


def diff(before: dict[str, Any] | None, after: dict[str, Any] | None, prefix: str = "") -> dict[str, Any]:
    """Fields that differ between two snapshots, as {field: {"from": old, "to": new}}.
    Nested dicts (e.g. custom_fields) are compared key by key, as 'custom_fields.owner'."""
    before, after = before or {}, after or {}
    out: dict[str, Any] = {}
    for key in sorted(set(before) | set(after)):
        if not prefix and key in _IGNORED_FIELDS:
            continue
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if isinstance(old, dict) and isinstance(new, dict):
            out.update(diff(old, new, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = {"from": old, "to": new}
    return out


def _snapshot(data: dict[str, Any] | None) -> dict[str, Any]:
    """Meaningful fields of a create/delete snapshot (no empties or zero counters)."""
    return {
        k: v
        for k, v in (data or {}).items()
        if k not in _IGNORED_FIELDS and v not in (None, "", [], {}) and not (k.endswith("_count") and v == 0)
    }


class RefLabeller:
    """Replaces foreign-key ids in changelog data (e.g. site: 3) with readable labels
    ('MCP Lab (id 3)'), using the schema to learn which fields reference which object type."""

    def __init__(self, nb: NetBoxClient, schema: SchemaIndex) -> None:
        self.nb = nb
        self.schema = schema
        self.by_ct = {content_type(t): t for t in schema.types.values()}
        self.by_model = {t.model: t for t in schema.types.values() if t.model}

    def target(self, ct: str | None, field: str) -> ObjectType | None:
        otype = self.by_ct.get(ct or "")
        if otype is None:
            return None
        # Writable schema: "IPAddress id (int), nullable". Read schema (covers read-only fields such
        # as an interface's cable): "Cable, nullable, read-only".
        for schema_name, pattern in ((otype.writable, _REF_RE), (otype.model, _READ_REF_RE)):
            summary = self.schema.schemas.get(schema_name or "", {}).get("fields", {}).get(field, "")
            if (m := pattern.match(summary)) and m.group(1) in self.by_model:
                return self.by_model[m.group(1)]
        return None

    async def apply(self, entries: list[dict[str, Any]]) -> None:
        # (container, key, target type, id) for every int value that references another object.
        slots: list[tuple[dict[str, Any], str, ObjectType, int]] = []
        for e in entries:
            ct = e.get("object_type")
            for field, change in (e.get("changes") or {}).items():
                if (t := self.target(ct, field)) is not None:
                    for side in ("from", "to"):
                        if isinstance(change[side], int):
                            slots.append((change, side, t, change[side]))
            data = e.get("data") or {}
            for field, value in data.items():
                if isinstance(value, int) and (t := self.target(ct, field)) is not None:
                    slots.append((data, field, t, value))
        wanted: dict[str, tuple[ObjectType, set[int]]] = {}
        for _, _, t, i in slots:
            wanted.setdefault(t.key, (t, set()))[1].add(i)
        names: dict[tuple[str, int], str] = {}

        async def fetch(t: ObjectType, ids: set[int]) -> None:
            try:
                objs, _ = await self.nb.list(t.path, {"id": sorted(ids), "brief": "true"}, limit=len(ids))
            except NetBoxError:
                return  # no permission for that type: leave the ids as they are
            for o in objs:
                names[(t.key, o["id"])] = label(o) or str(o["id"])

        await asyncio.gather(*(fetch(t, ids) for t, ids in wanted.values()))
        for container, key, t, i in slots:
            if (t.key, i) in names:
                container[key] = f"{names[(t.key, i)]} (id {i})"


def format_change(c: dict[str, Any]) -> dict[str, Any]:
    action = choice(c.get("action"))
    entry: dict[str, Any] = {
        "id": c["id"],
        "time": c.get("time"),
        "user": c.get("user_name") or label(c.get("user")),
        "action": action,
        "object_type": c.get("changed_object_type"),
        "object_id": c.get("changed_object_id"),
        "object": c.get("object_repr"),
        "message": c.get("message") or None,
        "request_id": c.get("request_id"),
    }
    if action == "update":
        entry["changes"] = diff(c.get("prechange_data"), c.get("postchange_data"))
    elif action == "create":
        entry["data"] = _snapshot(c.get("postchange_data"))
    elif action == "delete":
        entry["data"] = _snapshot(c.get("prechange_data"))
    return {k: v for k, v in entry.items() if v is not None}


async def _usernames(nb: NetBoxClient, ids: set[int]) -> dict[Any, str]:
    """User id -> username; empty if the token can't read users."""
    if not ids:
        return {}
    try:
        users, _ = await nb.list("users/users/", {"id": sorted(ids), "brief": "true"}, limit=len(ids))
    except NetBoxError:
        return {}
    return {u["id"]: u.get("username") or label(u) or str(u["id"]) for u in users}


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def netbox_change_history(
        ctx: Context,
        object_type: ObjectTypeArg = None,
        object: ObjectArg = None,
        include_related: Annotated[
            bool,
            Field(description="Also include changes to the object's components, e.g. a device's interfaces."),
        ] = True,
        user: Annotated[str | None, Field(description="Only changes made by this NetBox username.")] = None,
        action: Literal["create", "update", "delete"] | None = None,
        since: Annotated[
            str | None,
            Field(description="Start of the window: '30m', '24h', '7d', '2w' or an ISO date/time."),
        ] = None,
        until: Annotated[str | None, Field(description="End of the window (same formats as since).")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 25,
    ) -> dict[str, Any]:
        """NetBox's changelog: who changed what, and when, newest first. Updates show only the fields
        that changed (from -> to); creates and deletes show the object's data. Filter by object,
        object type, user, action and time window. Related objects are stored as ids in the data."""
        if object is not None and object_type is None:
            raise InvalidRequest(
                "object needs object_type too, e.g. object_type='devices', object='lab-sw1'."
            )
        nb = get_netbox(ctx)
        base: dict[str, Any] = {}
        if user:
            base["user_name"] = user
        if action:
            base["action"] = action
        if t := parse_time(since):
            base["time_after"] = t
        if t := parse_time(until):
            base["time_before"] = t

        schema = await get_schema(ctx)
        target: str | None = None
        queries: list[dict[str, Any]] = []
        if object_type is not None:
            otype = schema.resolve_type(object_type)
            ct = content_type(otype)
            if object is not None:
                obj = await resolve_object(nb, otype, object)
                target = f"{ct} {label(obj)} (id {obj['id']})"
                queries.append({**base, "changed_object_type": ct, "changed_object_id": obj["id"]})
                if include_related:
                    queries.append({**base, "related_object_type": ct, "related_object_id": obj["id"]})
            else:
                queries.append({**base, "changed_object_type": ct})
        else:
            queries.append(base)

        results = await asyncio.gather(*(nb.list("core/object-changes/", q, limit=limit) for q in queries))
        merged = {c["id"]: c for items, _ in results for c in items}
        changes = sorted(merged.values(), key=lambda c: str(c.get("time")), reverse=True)[:limit]
        total = sum(count for _, count in results)

        out: dict[str, Any] = {"returned": len(changes), "total": total}
        if target:
            out["object"] = target
        formatted = [format_change(c) for c in changes]
        await RefLabeller(nb, schema).apply(formatted)
        out["changes"] = formatted
        if total > len(changes):
            out["note"] = "More changes exist: narrow with since/until/user/action or raise limit."
        return out

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_journal(
        ctx: Context,
        object_type: ObjectTypeArg = None,
        object: ObjectArg = None,
        kind: Literal["info", "success", "warning", "danger"] | None = None,
        since: Annotated[str | None, Field(description="'24h', '7d', ... or an ISO date/time.")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 25,
    ) -> dict[str, Any]:
        """Journal entries (human notes) for an object, or the most recent entries across NetBox."""
        if object is not None and object_type is None:
            raise InvalidRequest("object needs object_type too.")
        nb = get_netbox(ctx)
        params: dict[str, Any] = {"ordering": "-created"}
        if kind:
            params["kind"] = kind
        if t := parse_time(since):
            params["created_after"] = t
        target = None
        if object_type is not None and object is not None:
            otype = (await get_schema(ctx)).resolve_type(object_type)
            obj = await resolve_object(nb, otype, object)
            params["assigned_object_type"] = content_type(otype)
            params["assigned_object_id"] = obj["id"]
            target = f"{content_type(otype)} {label(obj)} (id {obj['id']})"
        entries, total = await nb.list("extras/journal-entries/", params, limit=limit)
        users = await _usernames(
            nb, {e["created_by"] for e in entries if isinstance(e.get("created_by"), int)}
        )
        out: dict[str, Any] = {"returned": len(entries), "total": total}
        if target:
            out["object"] = target
        out["entries"] = [
            compact(
                {
                    "id": e["id"],
                    "created": e.get("created"),
                    "by": users.get(e.get("created_by")) or label(e.get("created_by")),
                    "kind": choice(e.get("kind")),
                    "object_type": e.get("assigned_object_type"),
                    "object": label(e.get("assigned_object")),
                    "comments": e.get("comments"),
                    "tags": [label(t) for t in e.get("tags") or []],
                }
            )
            for e in entries
        ]
        return out

    if not settings.netbox_mcp_enable_writes:
        return

    @mcp.tool(annotations=CREATE)
    async def netbox_add_journal_entry(
        ctx: Context,
        object_type: Annotated[str, Field(description="Object type, e.g. 'devices', 'ipam.prefixes'.")],
        object: Annotated[
            str | int, Field(description="The object's id or name / address / prefix / label.")
        ],
        comments: Annotated[str, Field(description="The note (Markdown supported).", min_length=1)],
        kind: Literal["info", "success", "warning", "danger"] = "info",
        confirm: Annotated[
            str | None,
            Field(
                description="Omit to preview. To apply, repeat the call with the same arguments "
                "plus confirm=<token>."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Add a journal entry (a dated note, e.g. 'replaced PSU') to any NetBox object (preview first)."""
        args = {"object_type": object_type, "object": object, "comments": comments, "kind": kind}
        nb = get_netbox(ctx)
        otype = (await get_schema(ctx)).resolve_type(object_type)
        obj = await resolve_object(nb, otype, object)
        ct = content_type(otype)
        plan = Plan(
            summary=f"Add a {kind} journal entry to {ct} {label(obj)}",
            details={"object": f"{ct} {label(obj)} (id {obj['id']})", "kind": kind, "comments": comments},
            fingerprint=obj["id"],
        )

        async def apply() -> dict[str, Any]:
            entry = await nb.request(
                "POST",
                "extras/journal-entries/",
                json={
                    "assigned_object_type": ct,
                    "assigned_object_id": obj["id"],
                    "kind": kind,
                    "comments": comments,
                },
            )
            audit.info(
                "caller=%s journal %s id=%s entry=%s", current_caller(), ct, obj["id"], entry.get("id")
            )
            return {"id": entry["id"], "created": entry.get("created")}

        return await preview_or_apply(
            get_previews(ctx),
            tool="netbox_add_journal_entry",
            args=args,
            confirm=confirm,
            caller=current_caller(),
            plan=plan,
            apply=apply,
        )
