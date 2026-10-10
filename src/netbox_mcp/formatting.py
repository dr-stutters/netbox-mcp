"""Shrink NetBox API responses for LLM consumption, and optionally render them as Markdown."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastmcp.tools import ToolResult

ResponseFormat = Literal["json", "markdown"]

# Hard cap on serialised output; beyond this, list results are cut and a hint is returned.
MAX_RESPONSE_CHARS = 60_000

_DROP_KEYS = {"url", "display_url", "_occupied"}
# Secrets NetBox stores in plain text (Wi-Fi PSKs, IKE pre-shared keys): masked so they don't end
# up in model transcripts. netbox_api_call with compact=False still returns raw data.
_SECRET_KEYS = {"auth_psk", "preshared_key"}
# Dropped from list results only (still shown by get-object): rarely useful, costly across many rows.
_LIST_DROP_KEYS = {"created", "last_updated"}


def _is_choice(d: dict[str, Any]) -> bool:
    return set(d) == {"value", "label"}


def _is_reference(d: dict[str, Any]) -> bool:
    # NetBox nested ("brief") objects always carry both id and url.
    return "id" in d and "url" in d


def compact(data: Any, *, top: bool = True, in_list: bool = False) -> Any:
    """Recursively trim a NetBox payload:
    - drop url/display_url, empty values (None, "", [], {}) and zero-valued *_count fields
    - choice fields {"value","label"} -> value
    - nested object references -> {"id", "display"} (plus a few identifying keys)
    - top-level `display` when it just repeats another field (name, prefix, address, ...)
    - secrets (Wi-Fi PSKs, IKE pre-shared keys) -> "********"
    - with in_list=True, also created/last_updated
    """
    if isinstance(data, list):
        return [compact(x, top=top, in_list=in_list) for x in data]
    if not isinstance(data, dict):
        return data
    if _is_choice(data):
        return data["value"]
    if not top and _is_reference(data):
        ref: dict[str, Any] = {"id": data["id"], "display": data.get("display")}
        # Keep the object-type hint for generic relations (e.g. assigned_object, cable terminations).
        for k in ("object_type", "device", "virtual_machine"):
            if k in data and data[k] is not None:
                ref[k] = compact(data[k], top=False)
        return ref
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k in _DROP_KEYS or (top and in_list and k in _LIST_DROP_KEYS):
            continue
        if k in _SECRET_KEYS and v:
            out[k] = "********"
            continue
        v = compact(v, top=False)
        if (
            v is None
            or v == ""
            or v == []
            or v == {}
            or (v == 0 and (k.endswith("_count") or k.startswith("count_")))
        ):
            continue
        out[k] = v
    if top and "display" in out and any(out["display"] == v for k, v in out.items() if k != "display"):
        del out["display"]
    return out


def compact_dict(data: dict[str, Any], *, in_list: bool = False) -> dict[str, Any]:
    """`compact` for a single object, typed as a dict."""
    result: dict[str, Any] = compact(data, in_list=in_list)
    return result


def _cell(v: Any) -> str:
    if isinstance(v, dict):
        v = v.get("display") or v.get("name") or v.get("id") or json.dumps(v, separators=(",", ":"))
    elif isinstance(v, list):
        v = ", ".join(_cell(x) for x in v)
    return str(v).replace("|", "\\|").replace("\n", " ")


def to_markdown(results: list[dict[str, Any]], max_cols: int = 10) -> str:
    if not results:
        return "_No results._"
    cols: list[str] = []
    for r in results:
        for k in r:
            if k not in cols:
                cols.append(k)
    cols = cols[:max_cols]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(_cell(r.get(c, "")) for c in cols) + " |" for r in results]
    return "\n".join(lines)


def _dumps(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), default=str)


def list_result(
    results: list[dict[str, Any]],
    *,
    count: int,
    offset: int,
    fmt: ResponseFormat = "json",
    use_compact: bool = True,
    extra: dict[str, Any] | None = None,
) -> ToolResult:
    """Package a page of results with pagination info, enforcing the size budget."""
    items = [compact(r, in_list=True) for r in results] if use_compact else results
    truncated = False
    while len(items) > 1 and len(_dumps(items)) > MAX_RESPONSE_CHARS:
        items = items[: max(1, len(items) * 3 // 4)]
        truncated = True
    meta: dict[str, Any] = {"count": count, "offset": offset, "returned": len(items)}
    if offset + len(items) < count:
        meta["next_offset"] = offset + len(items)
    if truncated:
        meta["note"] = (
            "Output truncated to fit size limit; use `fields`, `brief`, narrower filters or a smaller limit."
        )
    if extra:
        meta.update(extra)

    if fmt == "markdown":
        header = ", ".join(f"{k}: {v}" for k, v in meta.items())
        return ToolResult(content=f"{header}\n\n{to_markdown(items)}")
    payload = {**meta, "results": items}
    return ToolResult(content=_dumps(payload), structured_content=payload)


def object_result(obj: Any, *, fmt: ResponseFormat = "json", use_compact: bool = True) -> ToolResult:
    data = compact(obj) if use_compact else obj
    text = _dumps(data)
    if len(text) > MAX_RESPONSE_CHARS:
        text = text[:MAX_RESPONSE_CHARS] + '..."[truncated: use `fields` to select fewer fields]"'
        return ToolResult(content=text)
    if fmt == "markdown" and isinstance(data, dict):
        return ToolResult(content="\n".join(f"- **{k}**: {_cell(v)}" for k, v in data.items()))
    if isinstance(data, dict):
        return ToolResult(content=text, structured_content=data)
    return ToolResult(content=text)
