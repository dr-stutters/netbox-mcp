from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from netbox_mcp.config import Settings
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.server import create_server
from netbox_mcp.tools.common import _abbrev_match, normalise_mac
from netbox_mcp.tools.ipam import covered_addresses, usable_size

N = "http://netbox.test/api"


def page(*results: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"count": len(results), "next": None, "results": list(results)})


def ref(kind: str, id_: int, display: str, **extra: Any) -> dict[str, Any]:
    return {"id": id_, "url": f"{N}/{kind}/{id_}/", "display": display, **extra}


async def call(settings: Settings, tool: str, args: dict[str, Any]) -> Any:
    async with Client(create_server(settings)) as client:
        return (await client.call_tool(tool, args)).structured_content


# --- pure helpers ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff", "aabbccddeeff"])
def test_normalise_mac(raw: str) -> None:
    assert normalise_mac(raw) == "AA:BB:CC:DD:EE:FF"


def test_normalise_mac_rejects_garbage() -> None:
    with pytest.raises(InvalidRequest):
        normalise_mac("not-a-mac")


@pytest.mark.parametrize(
    ("short", "full", "ok"),
    [
        ("Gi1/0/1", "GigabitEthernet1/0/1", True),
        ("gi1/0/1", "GigabitEthernet1/0/1", True),
        ("Po1", "Port-channel1", True),
        ("Gi1/0/1", "GigabitEthernet1/0/10", False),
        ("Te1/0/1", "GigabitEthernet1/0/1", False),
    ],
)
def test_abbrev_match(short: str, full: str, ok: bool) -> None:
    assert _abbrev_match(short, full) is ok


def test_usable_size_and_coverage() -> None:
    import ipaddress

    assert usable_size(ipaddress.ip_network("10.0.0.0/24"), is_pool=False) == 254
    assert usable_size(ipaddress.ip_network("10.0.0.0/24"), is_pool=True) == 256
    assert usable_size(ipaddress.ip_network("10.0.0.0/31"), is_pool=False) == 2
    # Nested/overlapping children are only counted once.
    assert covered_addresses(["10.0.0.0/24", "10.0.0.0/25", "10.0.1.0/24", "2001:db8::/127"]) == 514


# --- tools ----------------------------------------------------------------------------------


async def test_lookup_ip(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/ipam/ip-addresses/").mock(
        return_value=page(
            {
                "id": 4,
                "address": "10.9.9.11/24",
                "status": {"value": "active", "label": "Active"},
                "vrf": ref("ipam/vrfs", 1, "LAB"),
                "assigned_object_type": "dcim.interface",
                "assigned_object": ref(
                    "dcim/interfaces", 10, "Vlan99", name="Vlan99", device=ref("dcim/devices", 3, "sw1")
                ),
            }
        )
    )
    netbox.get("/ipam/prefixes/").mock(
        return_value=page(
            {"id": 7, "prefix": "10.9.9.0/24", "_depth": 1, "vlan": ref("ipam/vlans", 3, "MGMT (99)")},
            {"id": 4, "prefix": "10.9.0.0/16", "_depth": 0},
        )
    )
    netbox.get("/ipam/ip-ranges/").mock(return_value=page())
    out = await call(settings, "netbox_lookup_ip", {"address": "10.9.9.11/24"})
    assert out["address"] == "10.9.9.11"
    assert out["ip_addresses"][0]["assigned_to"] == {
        "type": "dcim.interface",
        "interface": "Vlan99",
        "interface_id": 10,
        "device": "sw1",
    }
    assert [p["prefix"] for p in out["prefixes"]] == ["10.9.0.0/16", "10.9.9.0/24"]  # most specific last
    assert "note" not in out


async def test_lookup_ip_unrecorded(settings: Settings, netbox: respx.MockRouter) -> None:
    for path in ("/ipam/ip-addresses/", "/ipam/prefixes/", "/ipam/ip-ranges/"):
        netbox.get(path).mock(return_value=page())
    out = await call(settings, "netbox_lookup_ip", {"address": "192.0.2.1"})
    assert "not recorded" in out["note"]


async def test_lookup_ip_invalid(settings: Settings, netbox: respx.MockRouter) -> None:
    async with Client(create_server(settings)) as client:
        with pytest.raises(ToolError, match="not a valid IP"):
            await client.call_tool("netbox_lookup_ip", {"address": "10.1.2"})


async def test_lookup_mac(settings: Settings, netbox: respx.MockRouter) -> None:
    macs = netbox.get("/dcim/mac-addresses/").mock(
        return_value=page(
            {
                "id": 2,
                "mac_address": "52:54:00:99:00:11",
                "assigned_object_type": "dcim.interface",
                "assigned_object": ref(
                    "dcim/interfaces", 10, "Vlan99", name="Vlan99", device=ref("dcim/devices", 3, "sw1")
                ),
            }
        )
    )
    ips = netbox.get("/ipam/ip-addresses/").mock(return_value=page({"id": 4, "address": "10.9.9.11/24"}))
    out = await call(settings, "netbox_lookup_mac", {"mac": "5254.0099.0011"})
    assert macs.calls.last.request.url.params["mac_address"] == "52:54:00:99:00:11"
    assert ips.calls.last.request.url.params["interface_id"] == "10"
    assert out["matches"][0]["assigned_to"]["device"] == "sw1"
    assert out["matches"][0]["ip_addresses"] == ["10.9.9.11/24"]


async def test_device_summary(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/").mock(
        return_value=page(
            {
                "id": 3,
                "name": "sw1",
                "url": "u",
                "site": ref("dcim/sites", 2, "Lab"),
                "primary_ip": ref("ipam/ip-addresses", 4, "10.9.9.11/24"),
                "primary_ip4": ref("ipam/ip-addresses", 4, "10.9.9.11/24"),
                "config_context": {"huge": "blob"},
            }
        )
    )
    netbox.get("/dcim/interfaces/").mock(
        return_value=page(
            {
                "id": 6,
                "name": "Gi1/0/1",
                "type": {"value": "1000base-t", "label": "1000BASE-T"},
                "enabled": True,
                "mode": {"value": "tagged", "label": "Tagged"},
                "tagged_vlans": [{"id": 1, "vid": 10, "name": "USERS"}],
                "cable": ref("dcim/cables", 1, "C1"),
                "connected_endpoints": [{"id": 3, "name": "Gi2", "device": ref("dcim/devices", 2, "rtr1")}],
            },
            {"id": 7, "name": "Gi1/0/2", "enabled": False, "type": {"value": "1000base-t", "label": ""}},
            {"id": 10, "name": "Vlan99", "type": {"value": "virtual", "label": ""}},
        )
    )
    netbox.get("/ipam/ip-addresses/").mock(
        return_value=page({"address": "10.9.9.11/24", "assigned_object_id": 10})
    )

    out = await call(settings, "netbox_device_summary", {"name": "sw1"})
    assert out["kind"] == "device"
    assert "config_context" not in out
    assert "primary_ip" not in out and out["primary_ip4"]["display"] == "10.9.9.11/24"
    rows = {r["name"]: r for r in out["interfaces"]}
    assert rows["Gi1/0/1"] == {
        "id": 6,
        "name": "Gi1/0/1",
        "type": "1000base-t",
        "mode": "tagged",
        "tagged_vlans": [10],
        "cable": "C1",
        "peer": "rtr1 Gi2",
    }
    assert rows["Gi1/0/2"]["enabled"] is False
    assert rows["Vlan99"]["ips"] == ["10.9.9.11/24"]

    connected = await call(settings, "netbox_device_summary", {"name": "sw1", "connected_only": True})
    assert [r["name"] for r in connected["interfaces"]] == ["Gi1/0/1", "Vlan99"]


async def test_device_summary_falls_back_to_vm(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/").mock(return_value=page())
    netbox.get("/virtualization/virtual-machines/").mock(return_value=page({"id": 9, "name": "app1"}))
    out = await call(settings, "netbox_device_summary", {"name": "app1", "include_interfaces": False})
    assert out == {"kind": "vm", "id": 9, "name": "app1"}


TRACE = [
    [
        [
            {
                "id": 8,
                "url": f"{N}/dcim/interfaces/8/",
                "name": "Gi1/0/3",
                "device": ref("dcim/devices", 3, "sw1"),
            }
        ],
        {
            "id": 4,
            "label": "C4",
            "type": {"value": "cat6", "label": "CAT6"},
            "status": {"value": "connected", "label": ""},
        },
        [
            {
                "id": 1,
                "url": f"{N}/dcim/front-ports/1/",
                "name": "FP1",
                "device": ref("dcim/devices", 5, "pp1"),
            }
        ],
    ],
    [
        [{"id": 1, "url": f"{N}/dcim/rear-ports/1/", "name": "RP1", "device": ref("dcim/devices", 5, "pp1")}],
        {"id": 5, "label": ""},
        [
            {
                "id": 14,
                "url": f"{N}/dcim/interfaces/14/",
                "name": "Gi1/0/3",
                "device": ref("dcim/devices", 4, "sw2"),
            }
        ],
    ],
]


async def test_trace_cable_complete(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/interfaces/8/").mock(
        return_value=httpx.Response(
            200, json={"id": 8, "name": "Gi1/0/3", "device": ref("dcim/devices", 3, "sw1")}
        )
    )
    netbox.get("/dcim/interfaces/8/trace/").mock(return_value=httpx.Response(200, json=TRACE))
    out = await call(settings, "netbox_trace_cable", {"port_id": 8})
    assert out["complete"] is True
    assert out["far_end"] == "sw2 Gi1/0/3 (interface)"
    assert out["hops"] == [
        {"from": "sw1 Gi1/0/3 (interface)", "cable": "C4 (cat6, connected)", "to": "pp1 FP1 (front-port)"},
        {"from": "pp1 RP1 (rear-port)", "cable": "cable #5", "to": "sw2 Gi1/0/3 (interface)"},
    ]


async def test_trace_cable_incomplete_and_uncabled(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/interfaces/8/").mock(
        return_value=httpx.Response(
            200, json={"id": 8, "name": "Gi1/0/3", "device": ref("dcim/devices", 3, "sw1")}
        )
    )
    route = netbox.get("/dcim/interfaces/8/trace/").mock(return_value=httpx.Response(200, json=TRACE[:1]))
    out = await call(settings, "netbox_trace_cable", {"port_id": 8})
    assert out["complete"] is False and "incomplete" in out["note"]

    route.mock(return_value=httpx.Response(200, json=[]))
    out = await call(settings, "netbox_trace_cable", {"port_id": 8})
    assert out["cabled"] is False


async def test_interface_by_abbreviation(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/").mock(return_value=page({"id": 3, "name": "sw1"}))
    netbox.get("/dcim/interfaces/", params={"name": "Gi1/0/3"}).mock(return_value=page())
    netbox.get("/dcim/interfaces/").mock(
        return_value=page(
            {"id": 8, "name": "GigabitEthernet1/0/3", "device": ref("dcim/devices", 3, "sw1")},
            {"id": 9, "name": "GigabitEthernet1/0/30", "device": ref("dcim/devices", 3, "sw1")},
        )
    )
    netbox.get("/dcim/interfaces/8/trace/").mock(return_value=httpx.Response(200, json=[]))
    out = await call(settings, "netbox_trace_cable", {"device": "sw1", "port": "Gi1/0/3"})
    assert out["origin"] == "sw1 GigabitEthernet1/0/3"


async def test_prefix_utilization_active(settings: Settings, netbox: respx.MockRouter) -> None:
    prefix = {"id": 7, "prefix": "10.9.9.0/24", "status": {"value": "active", "label": ""}, "_depth": 1}
    netbox.get("/ipam/prefixes/", params={"prefix": "10.9.9.0/24"}).mock(return_value=page(prefix))
    netbox.get("/ipam/prefixes/", params={"within": "10.9.9.0/24"}).mock(return_value=page())
    netbox.get("/ipam/ip-addresses/").mock(
        return_value=httpx.Response(200, json={"count": 127, "next": "x", "results": [{"id": 1}]})
    )
    netbox.get("/ipam/prefixes/7/available-ips/").mock(
        return_value=httpx.Response(200, json=[{"address": "10.9.9.2/24"}])
    )
    out = await call(settings, "netbox_prefix_utilization", {"prefix": "10.9.9.0/24"})
    assert (out["size"], out["used"], out["utilization_pct"]) == (254, 127, 50.0)
    assert out["vrf"] == "global"
    assert out["first_available_ip"] == "10.9.9.2/24"


async def test_prefix_utilization_container(settings: Settings, netbox: respx.MockRouter) -> None:
    prefix = {"id": 4, "prefix": "10.9.0.0/16", "status": {"value": "container", "label": ""}, "_depth": 0}
    netbox.get("/ipam/prefixes/4/").mock(return_value=httpx.Response(200, json=prefix))
    netbox.get("/ipam/prefixes/").mock(
        return_value=page(
            {"id": 5, "prefix": "10.9.0.0/17", "_depth": 1},
            {"id": 6, "prefix": "10.9.0.0/24", "_depth": 2},  # nested: not double counted
        )
    )
    netbox.get("/ipam/prefixes/4/available-prefixes/").mock(
        return_value=httpx.Response(200, json=[{"prefix": "10.9.128.0/17"}])
    )
    out = await call(settings, "netbox_prefix_utilization", {"prefix": 4})
    assert out["utilization_pct"] == 50.0
    assert [c["prefix"] for c in out["child_prefixes"]] == ["10.9.0.0/17"]  # direct children only
    assert out["free_blocks"] == ["10.9.128.0/17"]


async def test_next_available_prefixes(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/ipam/prefixes/4/").mock(
        return_value=httpx.Response(200, json={"id": 4, "prefix": "10.9.0.0/16"})
    )
    netbox.get("/ipam/prefixes/4/available-prefixes/").mock(
        return_value=httpx.Response(200, json=[{"prefix": "10.9.1.128/25"}, {"prefix": "10.9.2.0/23"}])
    )
    out = await call(
        settings, "netbox_next_available_prefixes", {"prefix": 4, "prefix_length": 24, "count": 3}
    )
    assert out["available"] == ["10.9.2.0/24", "10.9.3.0/24"]  # the /25 is too small
    assert "Only 2" in out["note"]

    blocks = await call(settings, "netbox_next_available_prefixes", {"prefix": 4})
    assert blocks["free_blocks"] == ["10.9.1.128/25", "10.9.2.0/23"]


async def test_vlan_lookup(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/ipam/vlans/").mock(return_value=page({"id": 1, "vid": 10, "name": "USERS"}))
    netbox.get("/ipam/prefixes/").mock(return_value=page({"prefix": "10.9.10.0/24"}))
    netbox.get("/dcim/interfaces/").mock(
        return_value=page(
            {"name": "Gi1", "device": ref("dcim/devices", 3, "sw1"), "untagged_vlan": {"id": 1}},
            {"name": "Po1", "device": ref("dcim/devices", 3, "sw1"), "untagged_vlan": None},
        )
    )
    netbox.get("/virtualization/interfaces/").mock(return_value=page())
    out = await call(settings, "netbox_vlan_lookup", {"vid": 10})
    vlan = out["vlans"][0]
    assert vlan["prefixes"] == ["10.9.10.0/24"]
    assert vlan["interfaces"] == {"sw1": ["Gi1 (untagged)", "Po1 (tagged)"]}


async def test_search_groups_results(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/").mock(
        return_value=page({"id": 3, "url": "u", "display": "sw1", "name": "sw1"})
    )
    netbox.route(method="GET").mock(return_value=page())  # everything else: no matches
    out = await call(settings, "netbox_search", {"query": "sw1"})
    assert out["results"] == {"dcim.devices": {"count": 1, "results": [{"id": 3, "name": "sw1"}]}}
