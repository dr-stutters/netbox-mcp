"""NetBox OpenAPI schema: a compact index for object-type resolution, filter validation and
spec search. The raw schema (~15 MB) is fetched once, reduced to an index, and cached on disk
keyed by NetBox URL + version."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from netbox_mcp.client import NetBoxClient
from netbox_mcp.errors import InvalidRequest

log = logging.getLogger(__name__)

INDEX_FORMAT = 1
SCHEMA_TIMEOUT = 120.0

# Common shorthands that don't match an endpoint name.
ALIASES = {
    "vm": "virtualization.virtual-machines",
    "vms": "virtualization.virtual-machines",
    "ip": "ipam.ip-addresses",
    "ips": "ipam.ip-addresses",
    "ip-address": "ipam.ip-addresses",
    "address": "ipam.ip-addresses",
    "addresses": "ipam.ip-addresses",
    "subnet": "ipam.prefixes",
    "subnets": "ipam.prefixes",
}


@dataclass
class ObjectType:
    key: str  # e.g. "dcim.devices"
    path: str  # e.g. "dcim/devices/"
    description: str
    model: str | None  # read schema, e.g. "Device"
    writable: str | None  # create schema, e.g. "WritableDeviceRequest"
    filters: dict[str, list[str]] = field(default_factory=dict)  # base name -> lookup suffixes
    actions: list[str] = field(default_factory=list)  # e.g. ["trace/"] for {id}/trace/

    def filter_names(self) -> set[str]:
        names = set()
        for base, suffixes in self.filters.items():
            names.add(base)
            names.update(f"{base}__{s}" for s in suffixes)
        return names


@dataclass
class Operation:
    method: str
    path: str  # e.g. "/api/dcim/interfaces/{id}/trace/"
    operation_id: str
    description: str
    params: list[str]
    request: str | None
    response: str | None


@dataclass
class SchemaIndex:
    version: str
    types: dict[str, ObjectType]
    operations: list[Operation]
    schemas: dict[str, dict[str, Any]]  # name -> {"description", "required", "fields": {name: summary}}

    # --- object types -------------------------------------------------------------------

    def resolve_type(self, name: str) -> ObjectType:
        """Accepts 'dcim.devices', 'dcim/devices', '/api/dcim/devices/', 'devices', 'device'."""
        raw = name.strip().lower()
        key = re.sub(r"^/?(api/)?", "", raw).strip("/").replace("/", ".").replace("_", "-")
        key = ALIASES.get(key, key)
        if key in self.types:
            return self.types[key]
        if "." not in key:
            matches = [
                t for t in self.types.values() if t.key.split(".", 1)[1] in (key, f"{key}s", f"{key}es")
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                options = ", ".join(sorted(t.key for t in matches))
                raise InvalidRequest(f"Object type '{name}' is ambiguous: {options}")
        close = difflib.get_close_matches(key, list(self.types), n=5, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise InvalidRequest(
            f"Unknown object type '{name}'.{hint} Use netbox_list_object_types to see valid types."
        )

    def check_filters(self, otype: ObjectType, names: list[str]) -> None:
        """NetBox silently ignores unknown filters (returning everything), so reject them here."""
        valid = otype.filter_names() | {"q", "limit", "offset", "ordering", "brief", "fields", "omit"}
        bad = [n for n in names if n not in valid]
        if not bad:
            return
        problems = []
        for n in bad:
            close = difflib.get_close_matches(n, list(valid), n=3, cutoff=0.6)
            problems.append(f"'{n}'" + (f" (did you mean {', '.join(close)}?)" if close else ""))
        raise InvalidRequest(
            f"Unknown filter(s) for {otype.key}: {'; '.join(problems)}. "
            f"Use netbox_get_definition('{otype.key}', section='filters') to list valid filters."
        )

    def check_write_fields(self, otype: ObjectType, names: set[str], *, allow_id: bool = False) -> None:
        """NetBox silently ignores unknown fields on create/update, so reject them here."""
        if otype.writable is None or otype.writable not in self.schemas:
            raise InvalidRequest(f"{otype.key} does not support create/update via the API.")
        valid = set(self.schemas[otype.writable]["fields"]) | ({"id"} if allow_id else set())
        bad = sorted(names - valid)
        if not bad:
            return
        problems = []
        for n in bad:
            close = difflib.get_close_matches(n, list(valid), n=3, cutoff=0.6)
            problems.append(f"'{n}'" + (f" (did you mean {', '.join(close)}?)" if close else ""))
        raise InvalidRequest(
            f"Unknown field(s) for {otype.key}: {'; '.join(problems)}. "
            f"Use netbox_get_definition('{otype.key}', section='write') to list writable fields."
        )

    # --- search -------------------------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        terms = [t for t in re.split(r"[\s/._-]+", query.lower()) if t]
        if not terms:
            return []

        def score(primary: str, *rest: str) -> int:
            # Every term must appear somewhere; terms in the name/path weigh more than in descriptions.
            hay = " ".join((primary, *rest)).lower()
            if not all(t in hay for t in terms):
                return 0
            return sum(3 if t in primary.lower() else 1 for t in terms)

        hits: list[tuple[int, dict[str, Any]]] = []
        for t in self.types.values():
            if s := score(t.key, t.description, t.model or ""):
                hits.append((s + 5, {"kind": "object_type", "name": t.key, "description": t.description}))
        for op in self.operations:
            if s := score(op.path, op.operation_id, op.description):
                hits.append(
                    (
                        s,
                        {
                            "kind": "operation",
                            "name": f"{op.method} {op.path}",
                            "description": op.description[:160],
                        },
                    )
                )
        for name, sch in self.schemas.items():
            if s := score(name, sch.get("description", "")):
                hits.append((s - 1, {"kind": "schema", "name": name}))
        hits.sort(key=lambda h: -h[0])
        return [h[1] for h in hits[:limit]]

    # --- definitions --------------------------------------------------------------------

    def definition(self, name: str, section: str = "all") -> dict[str, Any]:
        if name in self.schemas:
            return {"schema": name, **self.schemas[name]}
        if "/" in name and ("{" in name or name.strip("/").count("/") > 2):
            path = "/api/" + re.sub(r"^/?(api/)?", "", name).strip("/") + "/"
            ops = [asdict(op) for op in self.operations if op.path == path]
            if not ops:
                raise InvalidRequest(f"No operations at {path}. Try netbox_search_spec.")
            return {"path": path, "operations": ops}

        otype = self.resolve_type(name)
        out: dict[str, Any] = {"object_type": otype.key, "path": f"/api/{otype.path}"}
        if otype.actions:
            out["extra_endpoints"] = [f"/api/{otype.path}{{id}}/{a}" for a in otype.actions]
        if section in ("all", "fields") and otype.model in self.schemas:
            out["fields"] = self.schemas[otype.model]["fields"]
        if section in ("all", "write") and otype.writable in self.schemas:
            w = self.schemas[otype.writable]
            out["writable_fields"] = w["fields"]
            out["required_for_create"] = w.get("required", [])
        if section in ("all", "filters"):
            out["filters"] = {b: s for b, s in otype.filters.items()}
            out["filter_note"] = (
                "Each filter accepts the base name or base__<lookup> (n=not, ic=contains, ie=iexact, "
                "isw=startswith, iew=endswith, gt/gte/lt/lte, empty). Repeat values via a list for OR."
            )
        return out


# --- building ------------------------------------------------------------------------------


def _ref_name(node: Any) -> str | None:
    if isinstance(node, dict):
        if "$ref" in node:
            return str(node["$ref"]).rsplit("/", 1)[-1]
        for k in ("allOf", "oneOf", "anyOf"):
            for sub in node.get(k, []):
                if r := _ref_name(sub):
                    return r
        if node.get("type") == "array":
            return _ref_name(node.get("items"))
    return None


def _summarise_prop(p: dict[str, Any]) -> str:
    parts: list[str] = []
    # Read-side choice fields are inline {"value", "label"} objects; the enum lives on "value".
    value_enum = p.get("properties", {}).get("value", {}).get("enum")
    if "enum" in p or value_enum:
        choices = [str(c) for c in (p.get("enum") or value_enum) if c not in ("", None)]
        parts.append("enum: " + "|".join(choices))
    elif "oneOf" in p and any(s.get("type") == "integer" for s in p["oneOf"]):
        ref = _ref_name(p)
        target = re.sub(r"^(Brief|Nested)|Request$", "", ref or "object")
        parts.append(f"{target} id (int)")
    elif ref := _ref_name(p):
        target = re.sub(r"^(Brief|Nested)|Request$", "", ref)
        if p.get("type") == "array":
            parts.append(f"list of {target}")
        else:
            # Nested value objects like {"value","label"} choice fields
            parts.append(target)
    else:
        t = p.get("type", "any")
        if t == "array":
            t = f"list of {p.get('items', {}).get('type', 'any')}"
        parts.append(t + (f" ({p['format']})" if "format" in p and p["format"] != "uri" else ""))
    if "maxLength" in p:
        parts.append(f"max {p['maxLength']}")
    if p.get("nullable"):
        parts.append("nullable")
    if p.get("readOnly"):
        parts.append("read-only")
    if (d := p.get("description")) and "enum" not in p:  # enum descriptions just repeat the choices
        parts.append(d.split("\n")[0][:100])
    return ", ".join(parts)


def _summarise_schema(s: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if d := s.get("description"):
        out["description"] = d[:200]
    if req := s.get("required"):
        out["required"] = req
    out["fields"] = {k: _summarise_prop(v) for k, v in s.get("properties", {}).items()}
    # Choice-field schemas carry their enum on the "value" property; surface it.
    if "enum" in s:
        out["enum"] = s["enum"]
    return out


def build_index(spec: dict[str, Any]) -> SchemaIndex:
    paths: dict[str, Any] = spec.get("paths", {})
    components: dict[str, Any] = spec.get("components", {}).get("schemas", {})

    types: dict[str, ObjectType] = {}
    operations: list[Operation] = []

    for path, item in paths.items():
        for method, op in item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            params = [p["name"] for p in op.get("parameters", []) if p.get("in") in ("query", "path")]
            is_list = method == "get" and "{" not in path and len(params) > 10
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            resp = next(
                (
                    r.get("content", {}).get("application/json", {}).get("schema")
                    for code, r in op.get("responses", {}).items()
                    if code.startswith("2")
                ),
                None,
            )
            operations.append(
                Operation(
                    method=method.upper(),
                    path=path,
                    operation_id=op.get("operationId", ""),
                    description=(op.get("description") or "").strip(),
                    params=["(filters: see object type)"] if is_list else params,
                    request=_ref_name(body),
                    response=_ref_name(resp),
                )
            )

        m = re.fullmatch(r"/api/([a-z-]+)/([a-z0-9-]+)/", path)
        if not m or "get" not in item:
            continue
        get = item["get"]
        resp_ref = _ref_name(
            get.get("responses", {})
            .get("200", {})
            .get("content", {})
            .get("application/json", {})
            .get("schema")
        )
        if not resp_ref or not resp_ref.startswith("Paginated"):
            continue  # not a model list endpoint (e.g. /api/status/)
        model = re.sub(r"^Paginated|List$", "", resp_ref)
        filters: dict[str, list[str]] = {}
        for p in get.get("parameters", []):
            n = p["name"]
            if n in ("limit", "offset", "ordering", "q", "brief", "fields", "omit"):
                continue
            base, _, suffix = n.partition("__")
            filters.setdefault(base, [])
            if suffix:
                filters[base].append(suffix)
        writable = None
        if "post" in item:
            post_body = item["post"].get("requestBody", {}).get("content", {}).get("application/json", {})
            writable = _ref_name(post_body.get("schema"))
        key = f"{m.group(1)}.{m.group(2)}"
        types[key] = ObjectType(
            key=key,
            path=f"{m.group(1)}/{m.group(2)}/",
            description=(get.get("description") or "").replace("Get a list of ", "").rstrip(".") or key,
            model=model if model in components else None,
            writable=writable,
            filters=filters,
        )

    for path in paths:
        m = re.fullmatch(r"/api/([a-z-]+)/([a-z0-9-]+)/\{id\}/(.+)", path)
        if m and (t := types.get(f"{m.group(1)}.{m.group(2)}")):
            t.actions.append(m.group(3))

    schemas = {
        name: _summarise_schema(s) for name, s in components.items() if not name.startswith("Paginated")
    }
    version = str(spec.get("info", {}).get("version", "unknown"))
    return SchemaIndex(version=version, types=types, operations=operations, schemas=schemas)


def _index_to_json(idx: SchemaIndex) -> dict[str, Any]:
    return {
        "format": INDEX_FORMAT,
        "version": idx.version,
        "types": {k: asdict(v) for k, v in idx.types.items()},
        "operations": [asdict(o) for o in idx.operations],
        "schemas": idx.schemas,
    }


def _index_from_json(d: dict[str, Any]) -> SchemaIndex:
    return SchemaIndex(
        version=d["version"],
        types={k: ObjectType(**v) for k, v in d["types"].items()},
        operations=[Operation(**o) for o in d["operations"]],
        schemas=d["schemas"],
    )


class SchemaStore:
    """Lazily loads the schema index on first use; safe for concurrent callers."""

    def __init__(self, client: NetBoxClient, cache_dir: Path | None) -> None:
        self._client = client
        self._cache_dir = cache_dir
        self._index: SchemaIndex | None = None
        self._lock = asyncio.Lock()

    async def get(self, *, refresh: bool = False) -> SchemaIndex:
        if self._index is not None and not refresh:
            return self._index
        async with self._lock:
            if self._index is not None and not refresh:
                return self._index
            self._index = await self._load(refresh)
            return self._index

    async def _load(self, refresh: bool) -> SchemaIndex:
        status = await self._client.status()
        version = str(status.get("netbox-full-version") or status.get("netbox-version") or "unknown")
        cache_file = None
        if self._cache_dir is not None:
            digest = hashlib.sha256(f"{self._client.base_url}|{version}|{INDEX_FORMAT}".encode()).hexdigest()[
                :16
            ]
            cache_file = self._cache_dir / f"schema-index-{digest}.json"
            if cache_file.exists() and not refresh:
                try:
                    data = json.loads(cache_file.read_text())
                    log.info("Loaded NetBox schema index from %s", cache_file)
                    return _index_from_json(data)
                except (ValueError, KeyError, TypeError) as e:
                    log.warning("Ignoring unreadable schema cache %s: %s", cache_file, e)

        log.info("Fetching NetBox OpenAPI schema (NetBox %s)", version)
        spec = await self._client.request("GET", "schema/", params={"format": "json"}, timeout=SCHEMA_TIMEOUT)
        index = build_index(spec)
        log.info("Indexed %d object types, %d operations", len(index.types), len(index.operations))
        if cache_file is not None:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(_index_to_json(index), separators=(",", ":")))
                tmp.replace(cache_file)
            except OSError as e:
                log.warning("Could not write schema cache %s: %s", cache_file, e)
        return index
