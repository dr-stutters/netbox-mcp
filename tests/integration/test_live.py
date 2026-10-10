"""Read-only checks against a live NetBox seeded with scripts/seed_lab.py.

uv run python scripts/seed_lab.py
uv run pytest tests/integration
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastmcp import Client

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.server import create_server

pytestmark = pytest.mark.integration


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[Client[Any]]:
    async with NetBoxClient(settings.netbox_url, settings.netbox_token.get_secret_value()) as nb:
        _, count = await nb.list("dcim/sites/", {"slug": "mcp-lab"}, limit=1)
    if not count:
        pytest.skip("Lab not seeded: run `uv run python scripts/seed_lab.py`")
    async with Client(create_server(settings)) as c:
        yield c


async def call(client: Client[Any], tool: str, **args: Any) -> Any:
    return (await client.call_tool(tool, args)).structured_content


async def test_status(client: Client[Any]) -> None:
    out = await call(client, "netbox_status")
    assert out["netbox_version"].startswith("4.")


async def test_trace_through_patch_panels(client: Client[Any]) -> None:
    out = await call(client, "netbox_trace_cable", device="lab-sw1", port="Gi1/0/3")
    assert out["complete"] is True
    assert out["hop_count"] == 3
    assert out["far_end"] == "lab-sw2 GigabitEthernet1/0/3 (interface)"


async def test_lookup_ip(client: Client[Any]) -> None:
    out = await call(client, "netbox_lookup_ip", address="10.99.99.11")
    assert out["ip_addresses"][0]["assigned_to"]["device"] == "lab-sw1"
    assert out["prefixes"][-1]["prefix"] == "10.99.99.0/24"


async def test_lookup_mac(client: Client[Any]) -> None:
    out = await call(client, "netbox_lookup_mac", mac="52-54-00-99-00-01")
    assert out["matches"][0]["assigned_to"]["device"] == "lab-rtr1"


async def test_device_summary(client: Client[Any]) -> None:
    out = await call(client, "netbox_device_summary", name="lab-sw1")
    rows = {r["name"]: r for r in out["interfaces"]}
    assert rows["GigabitEthernet1/0/1"]["peer"] == "lab-rtr1 GigabitEthernet2"
    assert rows["GigabitEthernet1/0/4"]["lag"] == "Port-channel1"
    assert rows["Vlan99"]["ips"] == ["10.99.99.11/24"]


async def test_interface_detail_lag(client: Client[Any]) -> None:
    out = await call(client, "netbox_interface_detail", device="lab-sw2", interface="Po1")
    assert out["lag_members"] == ["GigabitEthernet1/0/4"]
    assert "99 MGMT" in out["tagged_vlans"]


async def test_vlan_lookup(client: Client[Any]) -> None:
    out = await call(client, "netbox_vlan_lookup", vid=10, group="mcp-lab")
    vlan = out["vlans"][0]
    assert "GigabitEthernet1/0/2 (untagged)" in vlan["interfaces"]["lab-sw1"]


async def test_ipam_helpers(client: Client[Any]) -> None:
    util = await call(client, "netbox_prefix_utilization", prefix="10.99.99.0/24")
    assert util["used"] >= 3
    nxt = await call(client, "netbox_next_available_prefixes", prefix="10.99.255.0/24", prefix_length=31)
    assert nxt["available"] and nxt["available"][0] != "10.99.255.0/31"  # that one is taken


async def test_generic_list_with_filters(client: Client[Any]) -> None:
    out = await call(
        client, "netbox_list_objects", object_type="devices", filters={"site": "mcp-lab", "role": "switch"}
    )
    assert {r["name"] for r in out["results"]} == {"lab-sw1", "lab-sw2"}


async def test_change_history_labels_references(client: Client[Any]) -> None:
    out = await call(client, "netbox_change_history", object_type="devices", object="lab-sw1", limit=50)
    assert out["changes"], "seeded lab should have changelog entries"
    cable_changes = [
        c["changes"]["cable"]["to"]
        for c in out["changes"]
        if c["action"] == "update" and "cable" in c.get("changes", {})
    ]
    assert any(str(v).startswith("MCP-") for v in cable_changes)  # cable ids resolved to labels


async def test_site_summary(client: Client[Any]) -> None:
    out = await call(client, "netbox_site_summary", site="MCP Lab")
    assert out["counts"]["devices"] >= 5
    assert {r["rack"] for r in out["racks"]} >= {"R1", "R2"}
    assert "10 USERS" in out["vlans"]


async def test_rack_elevation_and_space(client: Client[Any]) -> None:
    r2 = await call(client, "netbox_rack_elevation", rack="R2", site="mcp-lab")
    assert {"device": "lab-sw2", "units": "U38", "face": "both"} in r2["devices"]
    assert r2["reservations"] == [{"units": "U20-U21", "description": "Reserved for lab firewall"}]

    space = await call(client, "netbox_find_rack_space", units=19, site="mcp-lab")
    by_rack = {r["rack"]: r for r in space["results"]}
    assert by_rack["R2"]["fits_at_position"] == [1]  # the reservation at U20-21 blocks anything higher


async def test_trace_through_circuit(client: Client[Any]) -> None:
    out = await call(client, "netbox_trace_cable", device="lab-rtr1", port="Gi4")
    assert out["complete"] is True
    assert out["far_end"] == "MCP Telco Backbone (provider-network)"


async def test_paths_through_patch_panel(client: Client[Any]) -> None:
    out = await call(client, "netbox_trace_cable", device="lab-pp1", port="RP1", port_type="rear-port")
    assert out["path_count"] == 1
    assert out["paths"][0]["endpoints"] == (
        "lab-sw1 GigabitEthernet1/0/3 (interface) <-> lab-sw2 GigabitEthernet1/0/3 (interface)"
    )


async def test_whats_connected(client: Client[Any]) -> None:
    out = await call(client, "netbox_whats_connected", device="lab-sw1", interface="Gi1/0/3")
    assert out["direct_peer"] == "lab-pp1 FP1 (front-port)"
    assert out["far_end"][0]["device"] == "lab-sw2"
    assert out["far_end"][0]["rack"] == "R2"


async def test_data_hygiene(client: Client[Any]) -> None:
    out = await call(client, "netbox_data_hygiene", site="MCP Lab")
    assert out["checks_run"] == 10
    unused = out["problems"].get("prefixes_unused", {}).get("examples", [])
    assert any(u.startswith("10.99.20.0/24") for u in unused)  # SERVERS has no IPs in the seed


async def test_next_available_vlans_asns_and_range_ips(client: Client[Any]) -> None:
    vlans = await call(client, "netbox_next_available_vlans", group="mcp-lab", count=2)
    assert len(vlans["available"]) == 2 and 10 not in vlans["available"]
    asns = await call(client, "netbox_next_available_asns", asn_range="mcp-lab-asns")
    assert asns["available"] == [64513]  # 64512 is taken in the seed
    ips = await call(client, "netbox_next_available_ips", ip_range="10.99.20.125")
    assert ips["available"] == ["10.99.20.100/24"]
