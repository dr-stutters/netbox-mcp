import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from netbox_mcp.config import Settings
from netbox_mcp.server import create_server

WRITE_TOOLS = {
    "netbox_create_object",
    "netbox_update_object",
    "netbox_delete_object",
    "netbox_allocate_ips",
    "netbox_allocate_prefix",
}


@pytest.fixture
def rw(settings: Settings) -> Settings:
    return settings.model_copy(update={"netbox_mcp_enable_writes": True})


def page(*results: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"count": len(results), "next": None, "results": list(results)})


def body(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


async def call(settings: Settings, tool: str, args: dict[str, Any]) -> Any:
    async with Client(create_server(settings)) as client:
        return (await client.call_tool(tool, args)).structured_content


async def tool_names(settings: Settings) -> set[str]:
    async with Client(create_server(settings)) as client:
        return {t.name for t in await client.list_tools()}


async def test_write_tools_hidden_unless_enabled(
    settings: Settings, rw: Settings, netbox: respx.MockRouter
) -> None:
    assert not (await tool_names(settings)) & WRITE_TOOLS
    assert await tool_names(rw) >= WRITE_TOOLS


async def test_create_rejects_unknown_fields(rw: Settings, netbox: respx.MockRouter) -> None:
    route = netbox.post("/dcim/devices/")
    async with Client(create_server(rw)) as client:
        with pytest.raises(ToolError, match=r"'stauts' \(did you mean status"):
            await client.call_tool(
                "netbox_create_object", {"object_type": "devices", "data": {"stauts": "x"}}
            )
    assert not route.called


async def test_create_single_normalises_tags(rw: Settings, netbox: respx.MockRouter) -> None:
    route = netbox.post("/dcim/devices/").mock(
        return_value=httpx.Response(201, json={"id": 9, "name": "sw3", "url": "u"})
    )
    out = await call(
        rw,
        "netbox_create_object",
        {"object_type": "devices", "data": {"name": "sw3", "site": 1, "tags": ["core", "Data Centre"]}},
    )
    assert out == {"id": 9, "name": "sw3"}
    assert body(route)["tags"] == [{"slug": "core"}, {"name": "Data Centre"}]


async def test_create_bulk(rw: Settings, netbox: respx.MockRouter) -> None:
    route = netbox.post("/dcim/devices/").mock(return_value=httpx.Response(201, json=[{"id": 1}, {"id": 2}]))
    out = await call(
        rw, "netbox_create_object", {"object_type": "devices", "data": [{"name": "a"}, {"name": "b"}]}
    )
    assert isinstance(body(route), list)
    assert out["created"] == 2


async def test_update_single_and_bulk(rw: Settings, netbox: respx.MockRouter) -> None:
    single = netbox.patch("/dcim/devices/5/").mock(
        return_value=httpx.Response(200, json={"id": 5, "status": "active"})
    )
    await call(rw, "netbox_update_object", {"object_type": "devices", "id": 5, "data": {"status": "active"}})
    assert body(single) == {"status": "active"}

    netbox.get("/dcim/devices/").mock(return_value=page({"id": 5}, {"id": 6}))
    bulk = netbox.patch("/dcim/devices/").mock(return_value=httpx.Response(200, json=[{"id": 5}, {"id": 6}]))
    out = await call(
        rw, "netbox_update_object", {"object_type": "devices", "ids": [6, 5, 6], "data": {"name": "x"}}
    )
    assert body(bulk) == [{"id": 5, "name": "x"}, {"id": 6, "name": "x"}]
    assert out["updated"] == 2


async def test_update_requires_exactly_one_target(rw: Settings, netbox: respx.MockRouter) -> None:
    async with Client(create_server(rw)) as client:
        with pytest.raises(ToolError, match="exactly one of id or ids"):
            await client.call_tool("netbox_update_object", {"object_type": "devices", "data": {"name": "x"}})


async def test_delete_all_or_nothing(rw: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/").mock(return_value=page({"id": 5, "display": "sw5"}))
    delete = netbox.delete("/dcim/devices/").mock(return_value=httpx.Response(204))
    async with Client(create_server(rw)) as client:
        with pytest.raises(ToolError, match=r"id\(s\) \[6\]; nothing was changed"):
            await client.call_tool("netbox_delete_object", {"object_type": "devices", "ids": [5, 6]})
    assert not delete.called

    out = await call(rw, "netbox_delete_object", {"object_type": "devices", "ids": [5]})
    assert body(delete) == [{"id": 5}]
    assert out == {"object_type": "dcim.devices", "deleted": [{"id": 5, "display": "sw5"}]}


async def test_allocate_ip_assign_and_set_primary(rw: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/ipam/prefixes/").mock(return_value=page({"id": 7, "prefix": "10.9.9.0/24"}))
    netbox.get("/dcim/devices/").mock(return_value=page({"id": 3, "name": "sw1"}))
    netbox.get("/dcim/interfaces/").mock(return_value=page({"id": 10, "name": "Vlan99", "device": {"id": 3}}))
    alloc = netbox.post("/ipam/prefixes/7/available-ips/").mock(
        return_value=httpx.Response(
            201, json=[{"id": 40, "address": "10.9.9.5/24", "status": {"value": "active"}}]
        )
    )
    primary = netbox.patch("/dcim/devices/3/").mock(return_value=httpx.Response(200, json={"id": 3}))

    out = await call(
        rw,
        "netbox_allocate_ips",
        {
            "prefix": "10.9.9.0/24",
            "device": "sw1",
            "interface": "Vlan99",
            "set_primary": True,
            "dns_name": "sw1.lab",
        },
    )
    assert body(alloc) == [
        {
            "status": "active",
            "dns_name": "sw1.lab",
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": 10,
        }
    ]
    assert body(primary) == {"primary_ip4": 40}
    assert out["allocated"][0]["address"] == "10.9.9.5/24"
    assert out["primary_set"] == "primary_ip4 of sw1"


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"device": "sw1"}, "both device and interface"),
        ({"device": "sw1", "interface": "Gi1", "count": 2}, "single IP"),
        ({"set_primary": True}, "set_primary needs"),
    ],
)
async def test_allocate_ip_argument_checks(
    rw: Settings, netbox: respx.MockRouter, args: dict[str, Any], error: str
) -> None:
    async with Client(create_server(rw)) as client:
        with pytest.raises(ToolError, match=error):
            await client.call_tool("netbox_allocate_ips", {"prefix": "10.9.9.0/24", **args})


async def test_allocate_prefix(rw: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/ipam/prefixes/4/").mock(
        return_value=httpx.Response(200, json={"id": 4, "prefix": "10.9.0.0/16"})
    )
    alloc = netbox.post("/ipam/prefixes/4/available-prefixes/").mock(
        return_value=httpx.Response(
            201, json=[{"id": 50, "prefix": "10.9.0.0/24"}, {"id": 51, "prefix": "10.9.1.0/24"}]
        )
    )
    out = await call(
        rw, "netbox_allocate_prefix", {"parent": 4, "prefix_length": 24, "count": 2, "description": "x"}
    )
    assert body(alloc) == [{"prefix_length": 24, "status": "active", "description": "x"}] * 2
    assert [a["prefix"] for a in out["allocated"]] == ["10.9.0.0/24", "10.9.1.0/24"]

    async with Client(create_server(rw)) as client:
        with pytest.raises(ToolError, match="cannot be carved"):
            await client.call_tool("netbox_allocate_prefix", {"parent": 4, "prefix_length": 16})


async def test_api_call_write_allowed_when_enabled(rw: Settings, netbox: respx.MockRouter) -> None:
    route = netbox.post("/extras/tags/").mock(return_value=httpx.Response(201, json={"id": 1, "name": "t"}))
    out = await call(
        rw, "netbox_api_call", {"path": "extras/tags/", "method": "POST", "body": {"name": "t", "slug": "t"}}
    )
    assert route.called
    assert out == {"id": 1, "name": "t"}
