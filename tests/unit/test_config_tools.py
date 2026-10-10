from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from netbox_mcp.config import Settings
from netbox_mcp.server import create_server
from netbox_mcp.tools.config import cf_filter_hint, context_matches


def page(*results: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"count": len(results), "next": None, "results": list(results)})


async def call(settings: Settings, tool: str, args: dict[str, Any]) -> Any:
    async with Client(create_server(settings)) as client:
        return (await client.call_tool(tool, args)).structured_content


def test_context_matches_and_or_semantics() -> None:
    host = {"sites": {6}, "roles": {16}, "regions": {1, 2}, "tags": {"core"}}
    assert context_matches({}, host) == []  # no criteria: applies everywhere
    assert context_matches({"sites": [{"id": 6}], "roles": [{"id": 16}, {"id": 99}]}, host) == [
        "sites",
        "roles",
    ]
    assert context_matches({"sites": [{"id": 6}], "roles": [{"id": 99}]}, host) is None  # AND across criteria
    assert context_matches({"regions": [{"id": 1}]}, host) == ["regions"]  # parent region (ancestor)
    assert context_matches({"tags": [{"slug": "core"}]}, host) == ["tags"]
    assert context_matches({"tags": [{"slug": "edge"}]}, host) is None


def test_cf_filter_hints() -> None:
    assert cf_filter_hint("img", "text", "loose") == "cf_img=part (matches anywhere, case-insensitive)"
    assert cf_filter_hint("img", "text", "exact") == "cf_img=exact value"
    assert cf_filter_hint("tier", "select", "loose") == "cf_tier=value, cf_tier__n=..."
    assert cf_filter_hint("x", "boolean", "disabled") == "filtering disabled"


async def test_render_config(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/").mock(return_value=page({"id": 3, "name": "sw1"}))
    render = netbox.post("/dcim/devices/3/render-config/").mock(
        return_value=httpx.Response(
            200, json={"configtemplate": {"id": 1, "display": "base"}, "content": "hostname sw1\nend"}
        )
    )
    out = await call(settings, "netbox_render_config", {"name": "sw1"})
    assert render.called
    assert out == {
        "kind": "device",
        "name": "sw1",
        "template": "base",
        "lines": 2,
        "config": "hostname sw1\nend",
    }


async def test_config_context_sources_with_region_ancestors(
    settings: Settings, netbox: respx.MockRouter
) -> None:
    netbox.get("/dcim/devices/").mock(return_value=page({"id": 3, "name": "sw1"}))
    netbox.get("/dcim/devices/3/").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 3,
                "name": "sw1",
                "site": {"id": 6},
                "role": {"id": 16},
                "config_context": {"ntp": ["b"], "domain": "lab"},
                "local_context_data": {"override": True},
            },
        )
    )
    netbox.get("/dcim/sites/6/").mock(return_value=httpx.Response(200, json={"id": 6, "region": {"id": 2}}))
    netbox.get("/dcim/regions/2/").mock(return_value=httpx.Response(200, json={"id": 2, "parent": {"id": 1}}))
    netbox.get("/dcim/regions/1/").mock(return_value=httpx.Response(200, json={"id": 1, "parent": None}))
    netbox.get("/dcim/device-roles/16/").mock(
        return_value=httpx.Response(200, json={"id": 16, "parent": None})
    )
    netbox.get("/extras/config-contexts/").mock(
        return_value=page(
            {"name": "switches", "weight": 2000, "roles": [{"id": 16}], "data": {"ntp": ["b"]}},
            {"name": "global", "weight": 1000, "data": {"domain": "lab"}},
            {"name": "apac", "weight": 1500, "regions": [{"id": 1}], "data": {"ntp": ["a"]}},
            {"name": "other-site", "weight": 1000, "sites": [{"id": 99}], "data": {"x": 1}},
        )
    )
    out = await call(settings, "netbox_config_context", {"name": "sw1"})
    assert [(s["name"], s["matched_on"]) for s in out["sources"]] == [
        ("global", ["(everything)"]),
        ("apac", ["regions"]),  # assigned to the parent region
        ("switches", ["roles"]),
    ]
    assert out["config_context"] == {"ntp": ["b"], "domain": "lab"}
    assert out["local_context_data"] == {"override": True}


async def test_custom_fields_with_choices(settings: Settings, netbox: respx.MockRouter) -> None:
    fields = netbox.get("/extras/custom-fields/").mock(
        return_value=page(
            {
                "name": "tier",
                "label": "Tier",
                "type": {"value": "select"},
                "object_types": ["dcim.device"],
                "filter_logic": {"value": "loose"},
                "choice_set": {"id": 1},
            }
        )
    )
    netbox.get("/extras/custom-field-choice-sets/1/choices/").mock(
        return_value=page({"id": "gold", "display": "Gold"}, {"id": "silver", "display": "Silver"})
    )
    out = await call(settings, "netbox_custom_fields", {"object_type": "devices"})
    assert fields.calls.last.request.url.params["object_type"] == "dcim.device"
    assert out["custom_fields"] == [
        {
            "name": "tier",
            "label": "Tier",
            "type": "select",
            "object_types": ["dcim.device"],
            "choices": ["gold", "silver"],
            "filter": "cf_tier=value, cf_tier__n=...",
        }
    ]


def cf_defs() -> httpx.Response:
    return page(
        {"name": "image", "type": {"value": "text"}, "filter_logic": {"value": "loose"}},
        {"name": "port", "type": {"value": "integer"}, "filter_logic": {"value": "loose"}},
        {"name": "secret", "type": {"value": "text"}, "filter_logic": {"value": "disabled"}},
    )


@pytest.mark.parametrize(
    ("filters", "error"),
    [
        ({"cf_imag": "x"}, r"no such custom field \(did you mean cf_image\?\)"),
        ({"cf_image__ic": "x"}, r"NetBox ignores __ic on a text field. Use cf_image=part"),
        ({"cf_port__ic": "8"}, r"ignores __ic on a integer field"),
        ({"cf_secret": "x"}, "filtering is disabled"),
    ],
)
async def test_list_objects_rejects_bad_cf_filters(
    settings: Settings, netbox: respx.MockRouter, filters: dict[str, Any], error: str
) -> None:
    netbox.get("/extras/custom-fields/").mock(return_value=cf_defs())
    devices = netbox.get("/dcim/devices/")
    async with Client(create_server(settings)) as client:
        with pytest.raises(ToolError, match=error):
            await client.call_tool("netbox_list_objects", {"object_type": "devices", "filters": filters})
    assert not devices.called


async def test_list_objects_accepts_valid_cf_filters(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/extras/custom-fields/").mock(return_value=cf_defs())
    devices = netbox.get("/dcim/devices/").mock(return_value=page({"id": 1, "name": "sw1"}))
    out = await call(
        settings,
        "netbox_list_objects",
        {"object_type": "devices", "filters": {"cf_image": "lscr", "cf_port__gte": 8000, "status": "active"}},
    )
    params = devices.calls.last.request.url.params
    assert (params["cf_image"], params["cf_port__gte"], params["status"]) == ("lscr", "8000", "active")
    assert out["count"] == 1
