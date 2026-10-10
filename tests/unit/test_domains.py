import json
from typing import Any

import httpx
import respx
from fastmcp import Client

from netbox_mcp.config import Settings
from netbox_mcp.formatting import compact
from netbox_mcp.server import create_server
from netbox_mcp.tools.domains import _watts

N = "http://netbox.test/api"


def page(*results: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"count": len(results), "next": None, "results": list(results)})


def ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json=data)


async def call(settings: Settings, tool: str, args: dict[str, Any]) -> Any:
    async with Client(create_server(settings)) as client:
        result = await client.call_tool(tool, args)
    return result.structured_content, result.content[0].text  # type: ignore[union-attr]


def dev(name: str) -> dict[str, Any]:
    return {"id": 1, "url": f"{N}/dcim/devices/1/", "display": name}


def test_feed_watts() -> None:
    assert (
        _watts({"voltage": 230, "amperage": 16, "max_utilization": 80, "phase": {"value": "single-phase"}})
        == 2944
    )
    assert (
        _watts({"voltage": 400, "amperage": 32, "max_utilization": 80, "phase": {"value": "three-phase"}})
        == 17736
    )


def test_compact_masks_secrets() -> None:
    assert compact({"id": 1, "ssid": "x", "auth_psk": "hunter2", "nested": {"preshared_key": "k"}}) == {
        "id": 1,
        "ssid": "x",
        "auth_psk": "********",
        "nested": {"preshared_key": "********"},
    }
    assert "auth_psk" not in compact({"id": 1, "auth_psk": ""})  # unset stays absent


async def test_circuit_summary(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/circuits/circuits/").mock(
        return_value=page(
            {"id": 5, "cid": "CID1", "provider": {"display": "Telco"}, "status": {"value": "active"}}
        )
    )
    netbox.get("/circuits/circuit-terminations/").mock(
        return_value=page(
            {
                "term_side": "Z",
                "termination_type": "circuits.providernetwork",
                "termination": {"display": "Backbone"},
            },
            {
                "term_side": "A",
                "termination_type": "dcim.site",
                "termination": {"display": "Lab"},
                "port_speed": 1000000,
                "cable": {"display": "C7"},
                "link_peers": [{"url": f"{N}/dcim/interfaces/4/", "name": "Gi4", "device": dev("rtr1")}],
            },
        )
    )
    out, _ = await call(settings, "netbox_circuit_summary", {"circuit": "CID1"})
    assert list(out["sides"]) == ["A", "Z"]
    assert out["sides"]["A"] == {
        "terminates_at": "Lab",
        "termination_type": "dcim.site",
        "port_speed_kbps": 1000000,
        "cable": "C7",
        "connected_to": "rtr1 Gi4 (interface)",
    }


async def test_tunnel_summary_never_returns_psk(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/vpn/tunnels/").mock(
        return_value=page(
            {"id": 1, "name": "TUN1", "encapsulation": {"value": "ipsec-tunnel"}, "ipsec_profile": {"id": 3}}
        )
    )
    netbox.get("/vpn/tunnel-terminations/").mock(
        return_value=page(
            {
                "role": {"value": "peer"},
                "termination": {"name": "Tunnel1", "device": dev("rtr1")},
                "outside_ip": {"display": "192.0.2.1/31"},
            }
        )
    )
    netbox.get("/vpn/ipsec-profiles/3/").mock(
        return_value=ok(
            {"name": "S2S", "mode": {"value": "esp"}, "ike_policy": {"id": 1}, "ipsec_policy": {"id": 2}}
        )
    )
    netbox.get("/vpn/ike-policies/1/").mock(
        return_value=ok(
            {"name": "IKEv2", "version": {"value": 2}, "preshared_key": "s3cret", "proposals": [{"id": 7}]}
        )
    )
    netbox.get("/vpn/ipsec-policies/2/").mock(return_value=ok({"name": "ESP", "proposals": []}))
    netbox.get("/vpn/ike-proposals/").mock(
        return_value=page(
            {"name": "P1", "encryption_algorithm": {"value": "aes-256-cbc"}, "group": {"value": 14}}
        )
    )
    out, text = await call(settings, "netbox_tunnel_summary", {"tunnel": "TUN1"})
    assert "s3cret" not in text
    assert out["endpoints"] == [{"role": "peer", "interface": "rtr1 Tunnel1", "outside_ip": "192.0.2.1/31"}]
    ike = out["ipsec"]["ike_policy"]
    assert ike["preshared_key_set"] is True
    assert ike["proposals"] == [{"name": "P1", "encryption": "aes-256-cbc", "dh_group": 14}]


async def test_l2vpn_by_identifier(settings: Settings, netbox: respx.MockRouter) -> None:
    l2 = netbox.get("/vpn/l2vpns/").mock(
        return_value=page({"id": 1, "name": "EVPN", "type": {"value": "vxlan-evpn"}, "identifier": 10020})
    )
    netbox.get("/vpn/l2vpn-terminations/").mock(
        return_value=page(
            {"assigned_object_type": "ipam.vlan", "assigned_object": {"display": "SERVERS (20)"}},
            {
                "assigned_object_type": "dcim.interface",
                "assigned_object": {"name": "Gi2", "device": dev("sw2")},
            },
        )
    )
    out, _ = await call(settings, "netbox_l2vpn_summary", {"l2vpn": "10020"})
    assert l2.calls[0].request.url.params["identifier"] == "10020"  # numbers are tried as identifiers first
    assert (out["vlans"], out["interfaces"], out["member_count"]) == (["SERVERS (20)"], ["sw2 Gi2"], 2)


async def test_wireless_lookup(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/wireless/wireless-lans/").mock(
        return_value=page(
            {"id": 1, "ssid": "Guest", "auth_type": {"value": "wpa-personal"}, "auth_psk": "pw123456"}
        )
    )
    netbox.get("/dcim/interfaces/").mock(
        return_value=page({"name": "wlan0", "device": dev("ap1"), "rf_role": {"value": "ap"}})
    )
    netbox.get("/wireless/wireless-links/").mock(return_value=page())
    out, text = await call(settings, "netbox_wireless_lookup", {"ssid": "gue"})
    assert "pw123456" not in text
    assert out["wireless_lans"] == [
        {"ssid": "Guest", "auth_type": "wpa-personal", "psk_set": True, "served_by": ["ap1 wlan0 (ap)"]}
    ]


async def test_power_trace_through_pdu_with_rack_budget(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/").mock(
        return_value=page({"id": 1, "name": "sw1", "rack": {"id": 9, "display": "R1"}})
    )
    psu = {"id": 2, "name": "PSU1", "device": dev("sw1"), "maximum_draw": 350, "allocated_draw": 120}
    inlet = {
        "id": 1,
        "name": "IN",
        "device": dev("pdu1"),
        "maximum_draw": 3680,
        "connected_endpoints_type": "dcim.powerfeed",
    }
    netbox.get("/dcim/power-ports/", params={"device_id": "1"}).mock(return_value=page(psu))
    netbox.get("/dcim/power-ports/2/trace/").mock(
        return_value=ok(
            [[[psu], {"id": 9}, [{"id": 5, "url": f"{N}/dcim/power-outlets/5/", "name": "Out1"}]]]
        )
    )
    netbox.get("/dcim/power-outlets/5/").mock(
        return_value=ok(
            {
                "id": 5,
                "name": "Out1",
                "device": dev("pdu1"),
                "feed_leg": {"value": "A"},
                "power_port": {"id": 1},
            }
        )
    )
    netbox.get("/dcim/power-ports/1/").mock(return_value=ok(inlet))
    netbox.get("/dcim/power-ports/1/trace/").mock(
        return_value=ok(
            [[[inlet], {"id": 8}, [{"id": 3, "url": f"{N}/dcim/power-feeds/3/", "power_panel": {}}]]]
        )
    )
    feed = {
        "id": 3,
        "name": "Feed A",
        "power_panel": {"display": "Panel A"},
        "type": {"value": "primary"},
        "voltage": 230,
        "amperage": 16,
        "max_utilization": 80,
    }
    netbox.get("/dcim/power-feeds/3/").mock(return_value=ok(feed))
    netbox.get("/dcim/power-feeds/").mock(return_value=page(feed))
    netbox.get("/dcim/power-ports/", params={"rack_id": "9"}).mock(return_value=page(psu, inlet))

    out, _ = await call(settings, "netbox_power_trace", {"device": "sw1"})
    assert out["power_ports"][0]["chain"] == [
        "sw1 PSU1 (power port, allocated 120 W, max 350 W)",
        "pdu1 Out1 (outlet, leg A)",
        "pdu1 IN (power port, max 3680 W)",
        "Feed A (feed, 230V 16A)",
        "Panel A (panel)",
    ]
    # The PDU inlet is excluded from the budget: its load is what's plugged into the PDU.
    assert out["rack_budget"] == {
        "rack": "R1",
        "feeds": ["Feed A (primary, 2944 W)"],
        "capacity_w": 2944,
        "allocated_draw_w": 120,
        "maximum_draw_w": 350,
        "utilization_pct": 4.1,
    }
    assert json.dumps(out)  # serialisable
