from typing import Any

import httpx
import respx
from fastmcp import Client

from netbox_mcp.config import Settings
from netbox_mcp.server import create_server
from netbox_mcp.tools.cabling import is_endpoint, route

N = "http://netbox.test/api"


def page(*results: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"count": len(results), "next": None, "results": list(results)})


def dev(id_: int, name: str) -> dict[str, Any]:
    return {"id": id_, "url": f"{N}/dcim/devices/{id_}/", "display": name}


def term(kind: str, id_: int, name: str, owner: dict[str, Any] | None = None) -> dict[str, Any]:
    t: dict[str, Any] = {"id": id_, "url": f"{N}/{kind}/{id_}/", "display": name, "name": name}
    if owner:
        t["device"] = owner
    return t


def cable(id_: int, lbl: str) -> dict[str, Any]:
    return {"id": id_, "url": f"{N}/dcim/cables/{id_}/", "display": lbl, "label": lbl}


SW1 = term("dcim/interfaces", 8, "Gi1/0/3", dev(1, "sw1"))
FP1 = term("dcim/front-ports", 1, "FP1", dev(5, "pp1"))
RP1 = term("dcim/rear-ports", 1, "RP1", dev(5, "pp1"))
SW2 = term("dcim/interfaces", 14, "Gi1/0/3", dev(2, "sw2"))
PATH = [[SW1], [cable(4, "C4")], [FP1], [RP1], [cable(5, "C5")], [SW2]]


async def call(settings: Settings, tool: str, args: dict[str, Any]) -> Any:
    async with Client(create_server(settings)) as client:
        return (await client.call_tool(tool, args)).structured_content


def test_route_and_endpoints() -> None:
    assert route(PATH) == [
        "sw1 Gi1/0/3 (interface)",
        "cable C4",
        "pp1 FP1 (front-port)",
        "pp1 RP1 (rear-port)",
        "cable C5",
        "sw2 Gi1/0/3 (interface)",
    ]
    assert is_endpoint([SW2])
    assert is_endpoint([term("circuits/provider-networks", 1, "Backbone")])
    assert not is_endpoint([RP1])
    assert not is_endpoint([])


async def test_trace_through_circuit_to_provider_network_is_complete(
    settings: Settings, netbox: respx.MockRouter
) -> None:
    netbox.get("/dcim/interfaces/49/").mock(
        return_value=httpx.Response(200, json={"id": 49, "name": "Gi4", "device": dev(3, "rtr1")})
    )
    term_a = {
        "id": 1,
        "url": f"{N}/circuits/circuit-terminations/1/",
        "term_side": "A",
        "circuit": {"display": "CID1"},
    }
    term_z = {
        "id": 2,
        "url": f"{N}/circuits/circuit-terminations/2/",
        "term_side": "Z",
        "circuit": {"display": "CID1"},
    }
    pn = term("circuits/provider-networks", 1, "Backbone")
    netbox.get("/dcim/interfaces/49/trace/").mock(
        return_value=httpx.Response(
            200,
            json=[
                [[term("dcim/interfaces", 49, "Gi4", dev(3, "rtr1"))], cable(7, "C7"), [term_a]],
                [[term_z], None, [pn]],
            ],
        )
    )
    out = await call(settings, "netbox_trace_cable", {"port_id": 49})
    assert out["complete"] is True  # last hop has no cable, but ends at a real endpoint
    assert out["far_end"] == "Backbone (provider-network)"
    assert out["hops"][1] == {
        "from": "CID1 term Z (circuit-termination)",
        "to": "Backbone (provider-network)",
    }


async def test_paths_from_rear_port_deduplicate_directions(
    settings: Settings, netbox: respx.MockRouter
) -> None:
    netbox.get("/dcim/devices/").mock(return_value=page({"id": 5, "name": "pp1"}))
    netbox.get("/dcim/rear-ports/").mock(return_value=page({"id": 1, "name": "RP1", "device": dev(5, "pp1")}))
    reverse = list(reversed(PATH))
    netbox.get("/dcim/rear-ports/1/paths/").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 35, "path": PATH, "is_complete": True, "is_active": True, "is_split": False},
                {"id": 36, "path": reverse, "is_complete": True, "is_active": True, "is_split": False},
            ],
        )
    )
    out = await call(
        settings, "netbox_trace_cable", {"device": "pp1", "port": "RP1", "port_type": "rear-port"}
    )
    assert out["path_count"] == 1
    assert out["paths"][0]["endpoints"] == "sw1 Gi1/0/3 (interface) <-> sw2 Gi1/0/3 (interface)"
    assert out["paths"][0]["complete"] is True


async def test_paths_from_circuit_termination_by_cid(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/circuits/circuits/").mock(return_value=page({"id": 9, "cid": "CID1"}))
    terms = netbox.get("/circuits/circuit-terminations/").mock(
        return_value=page({"id": 2, "term_side": "Z", "circuit": {"display": "CID1"}})
    )
    netbox.get("/circuits/circuit-terminations/2/paths/").mock(return_value=httpx.Response(200, json=[]))
    out = await call(
        settings, "netbox_trace_cable", {"port_type": "circuit-termination", "circuit": "CID1", "side": "Z"}
    )
    assert terms.calls.last.request.url.params["term_side"] == "Z"
    assert out == {
        "origin": "CID1 term Z",
        "path_count": 0,
        "paths": [],
        "note": "No cable paths pass through this port.",
    }


async def test_whats_connected_device_behind_patch_panel(
    settings: Settings, netbox: respx.MockRouter
) -> None:
    netbox.get("/dcim/interfaces/8/").mock(
        return_value=httpx.Response(
            200,
            json={
                **SW1,
                "cable": cable(4, "C4"),
                "link_peers_type": "dcim.frontport",
                "link_peers": [FP1],
                "connected_endpoints_type": "dcim.interface",
                "connected_endpoints": [SW2],
                "connected_endpoints_reachable": True,
            },
        )
    )
    netbox.get("/dcim/devices/").mock(
        return_value=page(
            {
                "id": 2,
                "name": "sw2",
                "role": {"display": "Switch"},
                "rack": {"display": "R2"},
                "position": 38.0,
                "primary_ip": {"display": "10.0.0.2/24"},
            }
        )
    )
    out = await call(settings, "netbox_whats_connected", {"interface_id": 8})
    assert out["direct_peer"] == "pp1 FP1 (front-port)"
    assert out["far_end"] == [
        {
            "device": "sw2",
            "interface": "Gi1/0/3",
            "role": "Switch",
            "rack": "R2",
            "position": 38,
            "primary_ip": "10.0.0.2/24",
        }
    ]


async def test_whats_connected_direct_and_uncabled(settings: Settings, netbox: respx.MockRouter) -> None:
    route_ = netbox.get("/dcim/interfaces/8/")
    route_.mock(
        return_value=httpx.Response(
            200,
            json={
                **SW1,
                "cable": cable(1, "C1"),
                "link_peers_type": "dcim.interface",
                "link_peers": [SW2],
                "connected_endpoints_type": "dcim.interface",
                "connected_endpoints": [SW2],
            },
        )
    )
    netbox.get("/dcim/devices/").mock(return_value=page({"id": 2, "name": "sw2"}))
    out = await call(settings, "netbox_whats_connected", {"interface_id": 8})
    assert "direct_peer" not in out  # cabled straight to the far end

    route_.mock(return_value=httpx.Response(200, json={**SW1, "cable": None, "connected_endpoints": None}))
    out = await call(settings, "netbox_whats_connected", {"interface_id": 8})
    assert out == {"interface": "sw1 Gi1/0/3", "connected": False, "note": "No cable attached."}
