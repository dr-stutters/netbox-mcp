import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from netbox_mcp.config import Settings
from netbox_mcp.server import create_server

from .conftest import preview_then_apply

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


async def tool_names(settings: Settings) -> set[str]:
    async with Client(create_server(settings)) as client:
        return {t.name for t in await client.list_tools()}


async def test_write_tools_hidden_unless_enabled(
    settings: Settings, rw: Settings, netbox: respx.MockRouter
) -> None:
    assert not (await tool_names(settings)) & WRITE_TOOLS
    assert await tool_names(rw) >= WRITE_TOOLS


async def test_create_rejects_unknown_fields(rw: Settings, netbox: respx.MockRouter) -> None:
    async with Client(create_server(rw)) as client:
        with pytest.raises(ToolError, match=r"'stauts' \(did you mean status"):
            await client.call_tool(
                "netbox_create_object", {"object_type": "devices", "data": {"stauts": "x"}}
            )


async def test_create_previews_then_applies(rw: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/sites/").mock(return_value=page({"id": 1, "url": "u", "display": "Lab"}))
    route = netbox.post("/dcim/devices/").mock(
        return_value=httpx.Response(201, json={"id": 9, "name": "sw3", "url": "u"})
    )
    args = {"object_type": "devices", "data": {"name": "sw3", "site": 1, "tags": ["core", "Data Centre"]}}
    async with Client(create_server(rw)) as client:
        preview, applied = await preview_then_apply(client, "netbox_create_object", args)
    assert preview["preview"]["objects"] == [
        {"name": "sw3", "site": "Lab (id 1)", "tags": ["core", "Data Centre"]}
    ]
    assert route.call_count == 1  # only the confirmed call writes
    assert body(route)["tags"] == [{"slug": "core"}, {"name": "Data Centre"}]
    assert applied["created"] == [{"id": 9, "name": "sw3"}]


async def test_preview_alone_never_writes(rw: Settings, netbox: respx.MockRouter) -> None:
    route = netbox.post("/dcim/devices/")
    async with Client(create_server(rw)) as client:
        out = (
            await client.call_tool(
                "netbox_create_object", {"object_type": "devices", "data": [{"name": "a"}]}
            )
        ).structured_content
    assert out["status"] == "preview" and out["confirm"].startswith("pv_")
    assert not route.called


async def test_update_preview_shows_diff_and_bulk_apply(rw: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/").mock(
        return_value=page(
            {
                "id": 5,
                "name": "sw5",
                "status": {"value": "planned", "label": "Planned"},
                "serial": "",
                "last_updated": "t1",
            },
            {
                "id": 6,
                "name": "sw6",
                "status": {"value": "active", "label": "Active"},
                "serial": "",
                "last_updated": "t1",
            },
        )
    )
    bulk = netbox.patch("/dcim/devices/").mock(return_value=httpx.Response(200, json=[{"id": 5}, {"id": 6}]))
    args = {"object_type": "devices", "ids": [6, 5, 6], "data": {"status": "active", "serial": "X1"}}
    async with Client(create_server(rw)) as client:
        preview, applied = await preview_then_apply(client, "netbox_update_object", args)
    objects = preview["preview"]["objects"]
    assert objects[0] == {
        "object": "sw5 (id 5)",
        "changes": {"serial": {"from": "(empty)", "to": "X1"}, "status": {"from": "planned", "to": "active"}},
    }
    assert objects[1] == {
        "object": "sw6 (id 6)",
        "changes": {"serial": {"from": "(empty)", "to": "X1"}},
        "unchanged": ["status"],
    }
    assert body(bulk) == [
        {"id": 5, "status": "active", "serial": "X1"},
        {"id": 6, "status": "active", "serial": "X1"},
    ]
    assert len(applied["updated"]) == 2


async def test_update_requires_exactly_one_target(rw: Settings, netbox: respx.MockRouter) -> None:
    async with Client(create_server(rw)) as client:
        with pytest.raises(ToolError, match="exactly one of id or ids"):
            await client.call_tool("netbox_update_object", {"object_type": "devices", "data": {"name": "x"}})


async def test_token_refused_when_args_differ_or_object_changed(
    rw: Settings, netbox: respx.MockRouter
) -> None:
    devices = netbox.get("/dcim/devices/").mock(
        return_value=page({"id": 5, "name": "sw5", "serial": "", "last_updated": "t1"})
    )
    patch = netbox.patch("/dcim/devices/5/")
    args = {"object_type": "devices", "id": 5, "data": {"serial": "A"}}
    async with Client(create_server(rw)) as client:
        token = (await client.call_tool("netbox_update_object", args)).structured_content["confirm"]
        with pytest.raises(ToolError, match="arguments differ"):
            await client.call_tool(
                "netbox_update_object", {**args, "data": {"serial": "B"}, "confirm": token}
            )
        with pytest.raises(ToolError, match="Unknown or expired"):  # a refused token is spent
            await client.call_tool("netbox_update_object", {**args, "confirm": token})

        token = (await client.call_tool("netbox_update_object", args)).structured_content["confirm"]
        devices.mock(
            return_value=page({"id": 5, "name": "sw5", "serial": "", "last_updated": "t2"})
        )  # edited meanwhile
        with pytest.raises(ToolError, match="changed since the preview"):
            await client.call_tool("netbox_update_object", {**args, "confirm": token})
    assert not patch.called


async def test_delete_preview_side_effects_and_blockers(rw: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/").mock(
        return_value=page(
            {"id": 5, "name": "sw5", "display": "sw5", "interface_count": 3, "last_updated": "t1"}
        )
    )
    netbox.get("/ipam/ip-addresses/").mock(
        return_value=httpx.Response(200, json={"count": 2, "next": None, "results": [{}]})
    )
    netbox.get("/ipam/services/").mock(return_value=page())
    netbox.get("/dcim/cables/").mock(
        return_value=httpx.Response(200, json={"count": 1, "next": None, "results": [{}]})
    )
    vms = netbox.get("/virtualization/virtual-machines/").mock(return_value=page())
    delete = netbox.delete("/dcim/devices/").mock(return_value=httpx.Response(204))
    async with Client(create_server(rw)) as client:
        preview, applied = await preview_then_apply(
            client, "netbox_delete_object", {"object_type": "devices", "ids": [5]}
        )
        assert preview["preview"]["side_effects"] == [
            "Deletes its components: 3 interfaces",
            "Deletes 2 IP address(es) assigned to its interfaces",
            "Leaves 1 cable(s) half-connected (NetBox keeps a cable when one end is deleted)",
        ]
        assert body(delete) == [{"id": 5}]
        assert applied["deleted"] == ["sw5 (id 5)"]

        vms.mock(return_value=httpx.Response(200, json={"count": 2, "next": None, "results": [{}]}))
        blocked = (
            await client.call_tool("netbox_delete_object", {"object_type": "devices", "ids": [5]})
        ).structured_content
        assert blocked["status"] == "blocked" and "confirm" not in blocked
        assert "2 VM(s) are hosted on it" in blocked["blockers"][0]


async def test_delete_missing_ids_changes_nothing(rw: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/").mock(return_value=page({"id": 5, "display": "sw5"}))
    async with Client(create_server(rw)) as client:
        with pytest.raises(ToolError, match=r"id\(s\) \[6\]; nothing was changed"):
            await client.call_tool("netbox_delete_object", {"object_type": "devices", "ids": [5, 6]})


async def test_allocate_ip_preview_shows_address_and_primary_change(
    rw: Settings, netbox: respx.MockRouter
) -> None:
    netbox.get("/ipam/prefixes/").mock(return_value=page({"id": 7, "prefix": "10.9.9.0/24"}))
    netbox.get("/ipam/prefixes/7/available-ips/").mock(
        return_value=httpx.Response(200, json=[{"address": "10.9.9.5/24"}])
    )
    netbox.get("/dcim/devices/", params={"name": "sw1"}).mock(return_value=page({"id": 3, "name": "sw1"}))
    netbox.get("/dcim/interfaces/").mock(return_value=page({"id": 10, "name": "Vlan99", "device": {"id": 3}}))
    netbox.get("/dcim/devices/3/").mock(
        return_value=httpx.Response(200, json={"id": 3, "primary_ip4": {"id": 1, "display": "10.9.9.4/24"}})
    )
    alloc = netbox.post("/ipam/prefixes/7/available-ips/").mock(
        return_value=httpx.Response(
            201, json=[{"id": 40, "address": "10.9.9.5/24", "status": {"value": "active"}}]
        )
    )
    primary = netbox.patch("/dcim/devices/3/").mock(return_value=httpx.Response(200, json={"id": 3}))
    args = {
        "prefix": "10.9.9.0/24",
        "device": "sw1",
        "interface": "Vlan99",
        "set_primary": True,
        "dns_name": "sw1.lab",
    }
    async with Client(create_server(rw)) as client:
        preview, applied = await preview_then_apply(client, "netbox_allocate_ips", args)
    assert preview["preview"] == {
        "prefix": "10.9.9.0/24",
        "addresses": ["10.9.9.5/24"],
        "status": "active",
        "assign_to": "sw1 Vlan99",
        "set_primary": {"field": "primary_ip4", "from": "10.9.9.4/24", "to": "10.9.9.5/24"},
    }
    assert body(alloc) == [
        {
            "status": "active",
            "dns_name": "sw1.lab",
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": 10,
        }
    ]
    assert body(primary) == {"primary_ip4": 40}
    assert applied["primary_set"] == "primary_ip4 of sw1"


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


async def test_allocate_prefix_preview_and_blocker(rw: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/ipam/prefixes/4/").mock(
        return_value=httpx.Response(200, json={"id": 4, "prefix": "10.9.0.0/16"})
    )
    free = netbox.get("/ipam/prefixes/4/available-prefixes/").mock(
        return_value=httpx.Response(200, json=[{"prefix": "10.9.0.0/23"}])
    )
    alloc = netbox.post("/ipam/prefixes/4/available-prefixes/").mock(
        return_value=httpx.Response(
            201, json=[{"id": 50, "prefix": "10.9.0.0/24"}, {"id": 51, "prefix": "10.9.1.0/24"}]
        )
    )
    args = {"parent": 4, "prefix_length": 24, "count": 2, "description": "x"}
    async with Client(create_server(rw)) as client:
        preview, applied = await preview_then_apply(client, "netbox_allocate_prefix", args)
        assert preview["preview"]["prefixes"] == ["10.9.0.0/24", "10.9.1.0/24"]
        assert body(alloc) == [{"prefix_length": 24, "status": "active", "description": "x"}] * 2
        assert [a["prefix"] for a in applied["allocated"]] == ["10.9.0.0/24", "10.9.1.0/24"]

        free.mock(return_value=httpx.Response(200, json=[{"prefix": "10.9.0.0/24"}]))
        blocked = (await client.call_tool("netbox_allocate_prefix", args)).structured_content
        assert blocked["status"] == "blocked" and blocked["blockers"] == [
            "Only 1 free /24 block(s) in 10.9.0.0/16"
        ]


async def test_api_call_write_previews_diff(rw: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/5/").mock(
        return_value=httpx.Response(
            200, json={"id": 5, "name": "sw5", "description": "", "last_updated": "t1"}
        )
    )
    patch = netbox.patch("/dcim/devices/5/").mock(
        return_value=httpx.Response(200, json={"id": 5, "description": "new"})
    )
    args = {"path": "dcim/devices/5/", "method": "PATCH", "body": {"description": "new"}}
    async with Client(create_server(rw)) as client:
        preview, applied = await preview_then_apply(client, "netbox_api_call", args)
    assert preview["preview"]["changes"] == {"description": {"from": "(empty)", "to": "new"}}
    assert patch.call_count == 1
    assert applied["response"] == {"id": 5, "description": "new"}
