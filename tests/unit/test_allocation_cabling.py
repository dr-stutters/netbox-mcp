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

N = "http://netbox.test/api"


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


DHCP = {
    "id": 1,
    "display": "10.9.20.100-150/24",
    "start_address": "10.9.20.100/24",
    "end_address": "10.9.20.150/24",
}
OTHER = {
    "id": 2,
    "display": "10.9.20.200-210/24",
    "start_address": "10.9.20.200/24",
    "end_address": "10.9.20.210/24",
}


def mock_range_lookup(netbox: respx.MockRouter) -> respx.Route:
    netbox.get("/ipam/prefixes/").mock(
        return_value=page({"prefix": "10.9.0.0/16"}, {"prefix": "10.9.20.0/24"})
    )
    return netbox.get("/ipam/ip-ranges/").mock(return_value=page(DHCP, OTHER))


async def test_next_ips_from_range_by_inner_address(settings: Settings, netbox: respx.MockRouter) -> None:
    ranges = mock_range_lookup(netbox)
    netbox.get("/ipam/ip-ranges/1/available-ips/").mock(
        return_value=httpx.Response(200, json=[{"address": "10.9.20.101/24"}])
    )
    out = await call(settings, "netbox_next_available_ips", {"ip_range": "10.9.20.120"})
    assert ranges.calls.last.request.url.params["parent"] == "10.9.0.0/16"  # outermost prefix
    assert out == {
        "ip_range": "10.9.20.100-150/24",
        "ip_range_id": 1,
        "vrf": "global",
        "available": ["10.9.20.101/24"],
    }


async def test_range_lookup_errors(settings: Settings, netbox: respx.MockRouter) -> None:
    mock_range_lookup(netbox)
    async with Client(create_server(settings)) as client:
        with pytest.raises(ToolError, match="No IP range contains 10.9.20.160"):
            await client.call_tool("netbox_next_available_ips", {"ip_range": "10.9.20.160"})
        with pytest.raises(ToolError, match="exactly one of prefix or ip_range"):
            await client.call_tool("netbox_next_available_ips", {})


async def apply(settings: Settings, tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    async with Client(create_server(settings)) as client:
        return await preview_then_apply(client, tool, args)


async def test_allocate_ip_from_range(rw: Settings, netbox: respx.MockRouter) -> None:
    mock_range_lookup(netbox)
    netbox.get("/ipam/ip-ranges/2/available-ips/").mock(
        return_value=httpx.Response(200, json=[{"address": "10.9.20.200/24"}])
    )
    post = netbox.post("/ipam/ip-ranges/2/available-ips/").mock(
        return_value=httpx.Response(201, json=[{"id": 7, "address": "10.9.20.200/24"}])
    )
    preview, out = await apply(rw, "netbox_allocate_ips", {"ip_range": "10.9.20.200-210", "status": "dhcp"})
    assert preview["preview"]["addresses"] == ["10.9.20.200/24"]
    assert body(post) == [{"status": "dhcp"}]
    assert out["allocated"] == [{"id": 7, "address": "10.9.20.200/24"}]


async def test_vlan_and_asn_peek(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/ipam/vlan-groups/", params={"name": "Lab"}).mock(
        return_value=page({"id": 4, "display": "Lab"})
    )
    netbox.get("/ipam/vlan-groups/4/available-vlans/").mock(
        return_value=httpx.Response(200, json=[{"vid": 1}, {"vid": 2}])
    )
    netbox.get("/ipam/asn-ranges/", params={"name": "Private"}).mock(
        return_value=page({"id": 1, "display": "Private"})
    )
    netbox.get("/ipam/asn-ranges/1/available-asns/").mock(
        return_value=httpx.Response(200, json=[{"asn": 64513}])
    )
    assert (await call(settings, "netbox_next_available_vlans", {"group": "Lab", "count": 2}))[
        "available"
    ] == [1, 2]
    assert (await call(settings, "netbox_next_available_asns", {"asn_range": "Private"}))["available"] == [
        64513
    ]


async def test_allocate_vlans_and_asns(rw: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/ipam/vlan-groups/4/").mock(
        return_value=httpx.Response(200, json={"id": 4, "display": "Lab"})
    )
    netbox.get("/ipam/vlan-groups/4/available-vlans/").mock(
        return_value=httpx.Response(200, json=[{"vid": 1}, {"vid": 2}])
    )
    vlans = netbox.post("/ipam/vlan-groups/4/available-vlans/").mock(
        return_value=httpx.Response(
            201, json=[{"id": 9, "vid": 1, "name": "GUEST"}, {"id": 10, "vid": 2, "name": "IOT"}]
        )
    )
    preview, out = await apply(
        rw, "netbox_allocate_vlans", {"group": 4, "names": ["GUEST", "IOT"], "tags": ["lab"]}
    )
    assert preview["preview"]["vlans"] == ["1 GUEST", "2 IOT"]
    assert body(vlans) == [
        {"name": "GUEST", "status": "active", "tags": [{"slug": "lab"}]},
        {"name": "IOT", "status": "active", "tags": [{"slug": "lab"}]},
    ]
    assert out["allocated"] == [{"id": 9, "vid": 1, "name": "GUEST"}, {"id": 10, "vid": 2, "name": "IOT"}]

    netbox.get("/ipam/asn-ranges/1/").mock(
        return_value=httpx.Response(200, json={"id": 1, "display": "Private"})
    )
    netbox.get("/ipam/asn-ranges/1/available-asns/").mock(
        return_value=httpx.Response(200, json=[{"asn": 64513}])
    )
    asns = netbox.post("/ipam/asn-ranges/1/available-asns/").mock(
        return_value=httpx.Response(201, json=[{"id": 3, "asn": 64513}])
    )
    preview, out = await apply(rw, "netbox_allocate_asns", {"asn_range": 1, "description": "edge"})
    assert preview["preview"]["asns"] == [64513]
    assert body(asns) == [{"description": "edge"}]
    assert out["allocated"] == [{"id": 3, "asn": 64513}]


SW1_GI5 = {"id": 50, "name": "Gi1/0/5", "device": {"id": 1, "display": "sw1"}, "cable": None}
PP1_FP2 = {"id": 6, "name": "FP2", "device": {"id": 5, "display": "pp1"}, "cable": None}


async def test_connect_cable_interface_to_front_port(rw: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/interfaces/50/").mock(return_value=httpx.Response(200, json=SW1_GI5))
    netbox.get("/dcim/front-ports/6/").mock(return_value=httpx.Response(200, json=PP1_FP2))
    post = netbox.post("/dcim/cables/").mock(
        return_value=httpx.Response(201, json={"id": 30, "label": "C30", "status": {"value": "connected"}})
    )
    netbox.get("/dcim/interfaces/50/trace/").mock(
        return_value=httpx.Response(
            200,
            json=[
                [
                    [{**SW1_GI5, "url": f"{N}/dcim/interfaces/50/"}],
                    {"id": 30},
                    [{**PP1_FP2, "url": f"{N}/dcim/front-ports/6/"}],
                ]
            ],
        )
    )
    preview, out = await apply(
        rw,
        "netbox_connect_cable",
        {
            "a_port_id": 50,
            "b_port_id": 6,
            "b_type": "front-port",
            "cable_label": "C30",
            "length": 2,
            "length_unit": "m",
        },
    )
    assert body(post) == {
        "a_terminations": [{"object_type": "dcim.interface", "object_id": 50}],
        "b_terminations": [{"object_type": "dcim.frontport", "object_id": 6}],
        "status": "connected",
        "label": "C30",
        "length": 2.0,
        "length_unit": "m",
    }
    assert preview["preview"]["a"] == "sw1 Gi1/0/5 (interface)"
    assert preview["preview"]["b"] == "pp1 FP2 (front-port)"
    assert post.call_count == 1
    assert out["path_complete"] is False  # ends at a patch panel front port for now


async def test_connect_cable_refusals(rw: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/interfaces/50/").mock(
        return_value=httpx.Response(
            200,
            json={
                **SW1_GI5,
                "cable": {"id": 1, "display": "OLD"},
                "link_peers": [
                    {"url": f"{N}/dcim/interfaces/9/", "name": "Gi2", "device": {"display": "rtr1"}}
                ],
            },
        )
    )
    netbox.get("/dcim/interfaces/51/").mock(return_value=httpx.Response(200, json={**SW1_GI5, "id": 51}))
    post = netbox.post("/dcim/cables/")
    async with Client(create_server(rw)) as client:
        blocked = (
            await client.call_tool("netbox_connect_cable", {"a_port_id": 50, "b_port_id": 51})
        ).structured_content
        assert blocked["status"] == "blocked"
        assert "already has cable OLD (to rtr1 Gi2 (interface))" in blocked["blockers"][0]
        with pytest.raises(ToolError, match="same port"):
            await client.call_tool("netbox_connect_cable", {"a_port_id": 51, "b_port_id": 51})
        with pytest.raises(ToolError, match="length_unit"):
            await client.call_tool("netbox_connect_cable", {"a_port_id": 51, "b_port_id": 50, "length": 3})
    assert not post.called


async def test_connect_cable_to_circuit_termination_by_cid(rw: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/interfaces/51/").mock(return_value=httpx.Response(200, json={**SW1_GI5, "id": 51}))
    netbox.get("/circuits/circuits/").mock(return_value=page({"id": 9, "cid": "CID1"}))
    terms = netbox.get("/circuits/circuit-terminations/").mock(
        return_value=page({"id": 3, "term_side": "Z", "circuit": {"display": "CID1"}, "cable": None})
    )
    post = netbox.post("/dcim/cables/").mock(return_value=httpx.Response(201, json={"id": 31}))
    netbox.get("/dcim/interfaces/51/trace/").mock(return_value=httpx.Response(200, json=[]))
    preview, _ = await apply(
        rw,
        "netbox_connect_cable",
        {"a_port_id": 51, "b_type": "circuit-termination", "b_device": "CID1", "b_port": "z"},
    )
    assert terms.calls.last.request.url.params["term_side"] == "Z"
    assert body(post)["b_terminations"] == [{"object_type": "circuits.circuittermination", "object_id": 3}]
    assert preview["preview"]["b"] == "CID1 term Z (circuit-termination)"


async def test_new_write_tools_are_gated(settings: Settings, netbox: respx.MockRouter) -> None:
    async with Client(create_server(settings)) as client:
        names = {t.name for t in await client.list_tools()}
    assert not names & {"netbox_allocate_vlans", "netbox_allocate_asns", "netbox_connect_cable"}
    assert {"netbox_next_available_vlans", "netbox_next_available_asns"} <= names
