from typing import Any

import httpx
import respx
from fastmcp import Client

from netbox_mcp.config import Settings
from netbox_mcp.server import create_server
from netbox_mcp.tools.sites import RackLayout, describe_layout, fmt_range, runs

N = "http://netbox.test/api"


def page(*results: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"count": len(results), "next": None, "results": list(results)})


def ref(kind: str, id_: int, display: str) -> dict[str, Any]:
    return {"id": id_, "url": f"{N}/{kind}/{id_}/", "display": display}


def elevation(height: int, devices: dict[int, str]) -> httpx.Response:
    """Half-unit elevation as NetBox returns it: U<n>.5 and U<n>, top down."""
    units = []
    for u in range(height, 0, -1):
        for frac in (0.5, 0.0):
            dev = devices.get(u)
            units.append({"id": u + frac, "device": {"id": 1, "url": "u", "display": dev} if dev else None})
    return page(*units)


async def call(settings: Settings, tool: str, args: dict[str, Any]) -> Any:
    async with Client(create_server(settings)) as client:
        return (await client.call_tool(tool, args)).structured_content


def test_runs_and_ranges() -> None:
    assert runs([5, 1, 2, 3, 7]) == [(1, 3), (5, 5), (7, 7)]
    assert fmt_range(4, 4) == "U4"
    assert fmt_range(1, 3) == "U1-U3"


def layout() -> RackLayout:
    return RackLayout(
        rack={"id": 1, "name": "R1"},
        units=list(range(10, 0, -1)),
        front={10: "pp1", 5: "sw1", 4: "sw1"},
        rear={10: "pp1", 2: "rear-pdu"},
        reserved={7: "firewall"},
    )


def test_layout_free_space_rules() -> None:
    lay = layout()
    assert lay.free_runs() == [(1, 1), (3, 3), (6, 6), (8, 9)]  # full depth, reserved excluded
    assert lay.free_runs(use_reserved=True) == [(1, 1), (3, 3), (6, 9)]
    assert lay.free_runs(full_depth=False, face="front") == [(1, 3), (6, 6), (8, 9)]
    assert lay.used == 4
    assert lay.utilization == 40.0


def test_describe_layout() -> None:
    out = describe_layout(layout())
    assert out["devices"] == [
        {"device": "pp1", "units": "U10", "face": "both"},
        {"device": "sw1", "units": "U4-U5", "face": "front"},
        {"device": "rear-pdu", "units": "U2", "face": "rear"},
    ]
    assert out["reservations"] == [{"units": "U7", "description": "firewall"}]
    assert out["free_ranges"] == ["U8-U9", "U6", "U3", "U1"]
    assert (out["used_u"], out["reserved_u"], out["free_u"]) == (4, 1, 5)


async def test_rack_elevation_tool(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/racks/").mock(
        return_value=page({"id": 1, "name": "R1", "site": ref("dcim/sites", 3, "Lab")})
    )
    netbox.get("/dcim/racks/1/elevation/").mock(return_value=elevation(4, {4: "pp1", 2: "sw1"}))
    netbox.get("/dcim/rack-reservations/").mock(return_value=page({"units": [1], "description": "fw"}))
    out = await call(settings, "netbox_rack_elevation", {"rack": "R1"})
    assert out["devices"] == [
        {"device": "pp1", "units": "U4", "face": "both"},
        {"device": "sw1", "units": "U2", "face": "both"},
    ]
    assert out["free_ranges"] == ["U3"]
    assert out["site"] == "Lab"


async def test_find_rack_space(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/sites/", params={"name": "Lab"}).mock(return_value=page({"id": 3, "name": "Lab"}))
    racks = netbox.get("/dcim/racks/").mock(
        return_value=page({"id": 1, "name": "R1"}, {"id": 2, "name": "R2"})
    )
    netbox.get("/dcim/racks/1/elevation/").mock(return_value=elevation(6, {6: "a", 3: "b"}))  # free 1-2, 4-5
    netbox.get("/dcim/racks/2/elevation/").mock(return_value=elevation(6, {6: "c"}))  # free 1-5
    netbox.get("/dcim/rack-reservations/").mock(return_value=page())
    out = await call(settings, "netbox_find_rack_space", {"units": 3, "site": "Lab"})
    assert racks.calls.last.request.url.params["site_id"] == "3"
    assert out["racks_checked"] == 2
    assert [(r["rack"], r["fits_at_position"], r["largest_free_block"]) for r in out["results"]] == [
        ("R2", [1, 2, 3], "U1-U5")
    ]


async def test_site_summary(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/sites/", params={"name": "Lab"}).mock(
        return_value=page({"id": 3, "name": "Lab", "slug": "lab", "status": {"value": "active"}})
    )
    netbox.get("/dcim/locations/").mock(return_value=page(ref("dcim/locations", 1, "DC1")))
    netbox.get("/dcim/racks/").mock(return_value=page({"id": 1, "name": "R1"}))
    netbox.get("/dcim/racks/1/elevation/").mock(return_value=elevation(4, {4: "sw1"}))
    netbox.get("/dcim/rack-reservations/").mock(return_value=page())
    netbox.get("/dcim/devices/").mock(
        return_value=page(
            {
                "id": 1,
                "name": "sw1",
                "role": ref("dcim/device-roles", 1, "Switch"),
                "status": {"value": "active"},
                "rack": ref("dcim/racks", 1, "R1"),
            },
            {
                "id": 2,
                "name": "rtr1",
                "role": ref("dcim/device-roles", 2, "Router"),
                "status": {"value": "planned"},
            },
        )
    )
    netbox.get("/virtualization/virtual-machines/").mock(
        return_value=page({"id": 9, "status": {"value": "active"}})
    )
    netbox.get("/ipam/prefixes/").mock(
        return_value=page({"prefix": "10.0.0.0/24", "status": {"value": "active"}})
    )
    netbox.get("/ipam/vlan-groups/").mock(return_value=page({"id": 5, "url": "u", "display": "G"}))
    netbox.get("/ipam/vlans/", params={"site_id": "3"}).mock(
        return_value=page({"id": 1, "vid": 10, "name": "USERS"})
    )
    netbox.get("/ipam/vlans/", params={"group_id": "5"}).mock(
        return_value=page({"id": 1, "vid": 10, "name": "USERS"}, {"id": 2, "vid": 99, "name": "MGMT"})
    )
    netbox.get("/circuits/circuit-terminations/").mock(return_value=page())

    out = await call(settings, "netbox_site_summary", {"site": "Lab"})
    assert out["counts"] == {
        "devices": 2,
        "racks": 1,
        "virtual_machines": 1,
        "prefixes": 1,
        "vlans": 2,
        "circuits": 0,
    }
    assert out["devices_by_role"] == {"Switch": 1, "Router": 1}
    assert out["devices_by_status"] == {"active": 1, "planned": 1}
    assert out["racks"] == [{"rack": "R1", "devices": 1, "utilization_pct": 25.0}]
    assert out["vlans"] == ["10 USERS", "99 MGMT"]  # de-duplicated across direct + group lookups
    assert out["devices"] == ["rtr1", "sw1"]
