from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from netbox_mcp.config import Settings
from netbox_mcp.server import create_server
from netbox_mcp.tools.analysis import compare_dicts, mermaid, short_if

N = "http://netbox.test/api"


def page(*results: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"count": len(results), "next": None, "results": list(results)})


def dev(id_: int, name: str) -> dict[str, Any]:
    return {"id": id_, "url": f"{N}/dcim/devices/{id_}/", "display": name}


def iface(
    id_: int, name: str, owner: dict[str, Any], peer: dict[str, Any] | None = None, **kw: Any
) -> dict[str, Any]:
    data: dict[str, Any] = {"id": id_, "name": name, "device": owner}
    if peer:
        data |= {"connected_endpoints_type": "dcim.interface", "connected_endpoints": [peer]}
    return data | kw


async def call(settings: Settings, tool: str, args: dict[str, Any]) -> Any:
    async with Client(create_server(settings)) as client:
        return (await client.call_tool(tool, args)).structured_content


def test_short_if() -> None:
    assert short_if("GigabitEthernet1/0/1") == "Gi1/0/1"
    assert short_if("TenGigabitEthernet0/1") == "Te0/1"
    assert short_if("Port-channel10") == "Po10"
    assert short_if("eth0") == "eth0"


def test_mermaid() -> None:
    text = mermaid({"sw-1": "Switch", "ISP": "providernetwork"}, [("sw-1", "ISP", 'Gi1 "up"')], {"ISP"})
    assert text.splitlines() == [
        "graph LR",
        '  n_ISP(["ISP<br/>providernetwork"])',
        '  n_sw_1["sw-1<br/>Switch"]',
        "  n_sw_1 ---|\"Gi1 'up'\"| n_ISP",
    ]


def test_compare_dicts_shows_missing_values() -> None:
    diff, same = compare_dicts({"rack": "R1", "serial": "A", "x": 1}, {"rack": "R2", "x": 1}, {"id"})
    assert diff == {"rack": {"a": "R1", "b": "R2"}, "serial": {"a": "A", "b": "(none)"}}
    assert same == 1


SW1, SW2, AP = dev(1, "sw1"), dev(2, "sw2"), dev(3, "ap1")


def links_for(device_id: str) -> httpx.Response:
    if device_id == "1":  # sw1: two links to sw2 (one via a patch panel), one to the AP
        return page(
            iface(10, "GigabitEthernet1/0/1", SW1, iface(20, "GigabitEthernet1/0/1", SW2)),
            iface(
                11,
                "GigabitEthernet1/0/3",
                SW1,
                iface(21, "GigabitEthernet1/0/3", SW2),
                link_peers_type="dcim.frontport",
                link_peers=[{"name": "FP1", "device": dev(9, "pp1")}],
            ),
            iface(12, "GigabitEthernet1/0/5", SW1, iface(30, "eth0", AP)),
        )
    if device_id == "2":  # sw2: links back to sw1 and an uplink elsewhere
        return page(
            iface(20, "GigabitEthernet1/0/1", SW2, iface(10, "GigabitEthernet1/0/1", SW1)),
            iface(22, "GigabitEthernet1/0/24", SW2, iface(40, "Gi0/0", dev(4, "core"))),
        )
    if device_id == "3":  # ap1: only connected to sw1
        return page(iface(30, "eth0", AP, iface(12, "GigabitEthernet1/0/5", SW1)))
    return page()


async def test_topology_around_device(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/", params={"name": "sw1"}).mock(return_value=page({"id": 1, "name": "sw1"}))
    netbox.get("/dcim/interfaces/").mock(side_effect=lambda r: links_for(r.url.params["device_id"]))
    netbox.get("/dcim/devices/").mock(
        return_value=page(
            {"id": 1, "name": "sw1", "role": {"display": "Switch"}},
            {"id": 2, "name": "sw2", "role": {"display": "Switch"}},
            {"id": 3, "name": "ap1", "role": {"display": "AP"}},
        )
    )
    out = await call(settings, "netbox_topology", {"device": "sw1"})
    assert out["node_count"] == 3 and out["edge_count"] == 3
    assert {"a": "sw1 GigabitEthernet1/0/3", "b": "sw2 GigabitEthernet1/0/3", "via": "pp1 FP1"} in out[
        "edges"
    ]
    assert '  n_sw1 ---|"Gi1/0/5 - eth0"| n_ap1' in out["mermaid"]
    assert '  n_ap1(["ap1<br/>AP"])' in out["mermaid"]  # outside the expanded scope


async def test_impact_flags_single_homed_and_power(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/", params={"name": "sw1"}).mock(return_value=page({"id": 1, "name": "sw1"}))
    netbox.get("/dcim/interfaces/").mock(side_effect=lambda r: links_for(r.url.params["device_id"]))
    netbox.get("/virtualization/virtual-machines/").mock(return_value=page())
    netbox.get("/ipam/ip-addresses/").mock(return_value=page())
    netbox.get("/ipam/services/").mock(return_value=page())
    netbox.get("/vpn/tunnel-terminations/").mock(return_value=page())
    netbox.get("/dcim/power-outlets/").mock(
        return_value=page({"name": "Out1", "connected_endpoints": [{"device": dev(3, "ap1")}]})
    )
    netbox.get("/dcim/power-ports/").mock(
        return_value=page({"device": {"id": 3}, "connected_endpoints": [{"device": {"id": 1}}]})
    )
    out = await call(settings, "netbox_impact_analysis", {"device": "sw1"})
    assert out["summary"]["single_homed_neighbours"] == ["ap1"]
    impacts = {n["device"]: n["impact"] for n in out["neighbours"]}
    assert impacts == {"sw2": "has other connections", "ap1": "SINGLE-HOMED: loses all connectivity"}
    assert out["powered_devices"] == ["ap1: loses power"]


async def test_compare_devices_with_interfaces(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/", params={"name": "sw1"}).mock(
        return_value=page(
            {"id": 1, "name": "sw1", "rack": {"id": 1, "url": "u", "display": "R1"}, "serial": "A1"}
        )
    )
    netbox.get("/dcim/devices/", params={"name": "sw2"}).mock(
        return_value=page({"id": 2, "name": "sw2", "rack": {"id": 2, "url": "u", "display": "R2"}})
    )
    netbox.get("/dcim/interfaces/", params={"device_id": "1"}).mock(
        return_value=page(
            {"name": "Gi1", "mode": {"value": "access"}, "untagged_vlan": {"vid": 10}},
            {"name": "Gi5", "mode": {"value": "access"}},
        )
    )
    netbox.get("/dcim/interfaces/", params={"device_id": "2"}).mock(
        return_value=page(
            {"name": "Gi1", "mode": {"value": "access"}, "untagged_vlan": {"vid": 20}}, {"name": "Tu1"}
        )
    )
    out = await call(settings, "netbox_compare", {"object_type": "devices", "a": "sw1", "b": "sw2"})
    assert out["differences"] == {"rack": {"a": "R1", "b": "R2"}, "serial": {"a": "A1", "b": "(none)"}}
    assert out["interfaces"] == {
        "only_on_a": ["Gi5"],
        "only_on_b": ["Tu1"],
        "same_name_count": 1,
        "differing": {"Gi1": {"untagged_vlan": {"a": 10, "b": 20}}},
    }


async def test_field_equals_value_references(settings: Settings, netbox: respx.MockRouter) -> None:
    devices = netbox.get("/dcim/devices/").mock(return_value=page({"id": 1, "name": "sw1"}))
    netbox.get("/dcim/interfaces/").mock(return_value=page())
    await call(settings, "netbox_compare", {"object_type": "devices", "a": "name=sw1", "b": "sw1"})
    assert devices.calls[0].request.url.params["name"] == "sw1"
    async with Client(create_server(settings)) as client:
        with pytest.raises(ToolError, match="can't be looked up by 'colour'"):
            await client.call_tool(
                "netbox_compare", {"object_type": "devices", "a": "colour=red", "b": "sw1"}
            )
