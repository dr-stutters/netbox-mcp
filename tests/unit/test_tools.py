import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from netbox_mcp.config import Settings
from netbox_mcp.server import create_server


async def test_list_objects_passes_filters(settings: Settings, netbox: respx.MockRouter) -> None:
    route = netbox.get("/dcim/devices/").mock(
        return_value=httpx.Response(
            200, json={"count": 1, "next": None, "results": [{"id": 1, "name": "core1", "url": "u"}]}
        )
    )
    async with Client(create_server(settings)) as client:
        res = await client.call_tool(
            "netbox_list_objects",
            {
                "object_type": "devices",
                "filters": {"status": ["active", "offline"], "site_id": 2},
                "fields": ["id", "name"],
                "limit": 10,
            },
        )
    params = route.calls.last.request.url.params
    assert params.get_list("status") == ["active", "offline"]
    assert params["site_id"] == "2"
    assert params["fields"] == "id,name"
    assert params["limit"] == "10"
    assert res.structured_content == {
        "count": 1,
        "offset": 0,
        "returned": 1,
        "object_type": "dcim.devices",
        "results": [{"id": 1, "name": "core1"}],
    }


async def test_list_objects_rejects_unknown_filter(settings: Settings, netbox: respx.MockRouter) -> None:
    route = netbox.get("/dcim/devices/")
    async with Client(create_server(settings)) as client:
        with pytest.raises(ToolError, match="Unknown filter.*stauts"):
            await client.call_tool(
                "netbox_list_objects", {"object_type": "devices", "filters": {"stauts": "x"}}
            )
    assert not route.called


async def test_get_object(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/ipam/prefixes/3/").mock(
        return_value=httpx.Response(200, json={"id": 3, "prefix": "10.0.0.0/24"})
    )
    async with Client(create_server(settings)) as client:
        res = await client.call_tool("netbox_get_object", {"object_type": "prefix", "id": 3})
    assert res.structured_content == {"id": 3, "prefix": "10.0.0.0/24"}


async def test_not_found_message(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/ipam/prefixes/99/").mock(
        return_value=httpx.Response(404, json={"detail": "No Prefix matches"})
    )
    async with Client(create_server(settings)) as client:
        with pytest.raises(ToolError, match="Not found"):
            await client.call_tool("netbox_get_object", {"object_type": "prefix", "id": 99})


async def test_list_object_types(settings: Settings, netbox: respx.MockRouter) -> None:
    async with Client(create_server(settings)) as client:
        res = await client.call_tool("netbox_list_object_types", {"app": "ipam"})
    assert res.structured_content == {
        "ipam": {
            "asn-ranges": "ASN range objects",
            "prefixes": "prefix objects",
            "vlan-groups": "VLAN group objects",
        }
    }


async def test_api_call_read_only_by_default(settings: Settings, netbox: respx.MockRouter) -> None:
    route = netbox.post("/dcim/devices/")
    async with Client(create_server(settings)) as client:
        with pytest.raises(ToolError, match="read-only"):
            await client.call_tool("netbox_api_call", {"path": "dcim/devices/", "method": "POST", "body": {}})
    assert not route.called


@pytest.mark.parametrize("path", ["../admin/", "dcim/devices/?id=1", "/api/../../etc"])
async def test_api_call_rejects_bad_paths(settings: Settings, netbox: respx.MockRouter, path: str) -> None:
    async with Client(create_server(settings)) as client:
        with pytest.raises(ToolError, match="Invalid API path"):
            await client.call_tool("netbox_api_call", {"path": path})


async def test_api_call_get_paginated(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/ipam/prefixes/1/available-ips/").mock(
        return_value=httpx.Response(200, json=[{"family": 4, "address": "10.0.0.1/24"}])
    )
    async with Client(create_server(settings)) as client:
        res = await client.call_tool("netbox_api_call", {"path": "ipam/prefixes/1/available-ips"})
    assert res.structured_content == {
        "count": 1,
        "offset": 0,
        "returned": 1,
        "results": [{"family": 4, "address": "10.0.0.1/24"}],
    }


async def test_search_spec_and_definition(settings: Settings, netbox: respx.MockRouter) -> None:
    async with Client(create_server(settings)) as client:
        hits = await client.call_tool("netbox_search_spec", {"query": "trace"})
        definition = await client.call_tool("netbox_get_definition", {"name": "devices", "section": "write"})
    assert "GET /api/dcim/interfaces/{id}/trace/" in hits.content[0].text  # type: ignore[union-attr]
    assert definition.structured_content is not None
    assert definition.structured_content["required_for_create"] == ["site"]
