"""Write-path checks against the seeded lab. They create and then delete their own objects
(tagged mcp-test). Opt-in, because they change live data:

uv run python scripts/seed_lab.py
NETBOX_MCP_TEST_WRITES=1 uv run pytest tests/integration
"""

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from netbox_mcp.config import Settings
from netbox_mcp.server import create_server

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("NETBOX_MCP_TEST_WRITES") != "1", reason="set NETBOX_MCP_TEST_WRITES=1"
    ),
]


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[Client[Any]]:
    async with Client(create_server(settings.model_copy(update={"netbox_mcp_enable_writes": True}))) as c:
        sites = await c.call_tool(
            "netbox_list_objects", {"object_type": "sites", "filters": {"slug": "mcp-lab"}}
        )
        if not sites.structured_content or not sites.structured_content["count"]:
            pytest.skip("Lab not seeded: run `uv run python scripts/seed_lab.py`")
        yield c


async def call(client: Client[Any], tool: str, **args: Any) -> Any:
    return (await client.call_tool(tool, args)).structured_content


async def test_device_lifecycle(client: Client[Any]) -> None:
    sw1 = (await call(client, "netbox_list_objects", object_type="devices", filters={"name": "lab-sw1"}))[
        "results"
    ][0]
    data = {
        "name": "lab-it-device",
        "site": sw1["site"]["id"],
        "role": sw1["role"]["id"],
        "device_type": sw1["device_type"]["id"],
        "status": "planned",
        "tags": ["mcp-test"],
    }
    dev = await call(client, "netbox_create_object", object_type="devices", data=data)
    try:
        with pytest.raises(ToolError, match="Unknown field"):
            await call(
                client, "netbox_update_object", object_type="devices", id=dev["id"], data={"stauts": "x"}
            )
        updated = await call(
            client, "netbox_update_object", object_type="devices", id=dev["id"], data={"status": "active"}
        )
        assert updated["status"] == "active"
    finally:
        out = await call(client, "netbox_delete_object", object_type="devices", ids=[dev["id"]])
        assert out["deleted"][0]["display"] == "lab-it-device"


async def test_allocate_and_release_ip(client: Client[Any]) -> None:
    before = await call(client, "netbox_next_available_ips", prefix="10.99.20.0/24")
    out = await call(
        client,
        "netbox_allocate_ips",
        prefix="10.99.20.0/24",
        description="integration test",
        tags=["mcp-test"],
    )
    ip = out["allocated"][0]
    try:
        assert ip["address"] == before["available"][0]
    finally:
        await call(client, "netbox_delete_object", object_type="ip-addresses", ids=[ip["id"]])


async def test_journal_round_trip(client: Client[Any]) -> None:
    added = await call(
        client,
        "netbox_add_journal_entry",
        object_type="devices",
        object="lab-sw1",
        comments="integration test",
    )
    try:
        out = await call(client, "netbox_journal", object_type="devices", object="lab-sw1")
        assert any(e["id"] == added["id"] and e["comments"] == "integration test" for e in out["entries"])
    finally:
        await call(client, "netbox_api_call", path=f"extras/journal-entries/{added['id']}/", method="DELETE")


async def test_data_hygiene_finds_planted_problems(client: Client[Any]) -> None:
    sw1 = (await call(client, "netbox_list_objects", object_type="devices", filters={"name": "lab-sw1"}))[
        "results"
    ][0]
    # NetBox enforces unique IPs per VRF by default, so duplicates need a VRF with that turned off.
    vrf = await call(
        client,
        "netbox_create_object",
        object_type="vrfs",
        data={"name": "MCP-DUP-TEST", "enforce_unique": False, "tags": ["mcp-test"]},
    )
    ips = await call(
        client,
        "netbox_create_object",
        object_type="ip-addresses",
        data=[{"address": "198.51.100.7/24", "vrf": vrf["id"], "tags": ["mcp-test"]}] * 2,
    )
    trunk = await call(
        client,
        "netbox_create_object",
        object_type="dcim.interfaces",
        data={"device": sw1["id"], "name": "GigabitEthernet1/0/48", "type": "1000base-t", "mode": "tagged"},
    )
    try:
        everywhere = await call(
            client, "netbox_data_hygiene", checks=["duplicate_ips", "ips_outside_prefixes"]
        )
        assert any(
            e.startswith("198.51.100.7 (MCP-DUP-TEST)")
            for e in everywhere["problems"]["duplicate_ips"]["examples"]
        )
        assert everywhere["problems"]["ips_outside_prefixes"]["count"] >= 2
        site = await call(client, "netbox_data_hygiene", site="MCP Lab", checks=["trunks_without_cable"])
        assert "lab-sw1 GigabitEthernet1/0/48" in site["problems"]["trunks_without_cable"]["examples"]
    finally:
        await call(
            client, "netbox_delete_object", object_type="ip-addresses", ids=[i["id"] for i in ips["results"]]
        )
        await call(client, "netbox_delete_object", object_type="vrfs", ids=[vrf["id"]])
        await call(client, "netbox_delete_object", object_type="dcim.interfaces", ids=[trunk["id"]])


async def test_allocations_and_connect_cable(client: Client[Any]) -> None:
    ip = await call(client, "netbox_allocate_ips", ip_range="10.99.20.100", status="dhcp", tags=["mcp-test"])
    vlan = await call(client, "netbox_allocate_vlans", group="mcp-lab", names=["IT-TEMP"], tags=["mcp-test"])
    asn = await call(client, "netbox_allocate_asns", asn_range="mcp-lab-asns", tags=["mcp-test"])
    cable = await call(
        client,
        "netbox_connect_cable",
        a_device="lab-sw1",
        a_port="Gi1/0/2",
        b_device="lab-sw2",
        b_port="Gi1/0/2",
        cable_label="MCP-IT",
        tags=["mcp-test"],
    )
    try:
        assert ip["allocated"][0]["address"].startswith("10.99.20.1")
        assert vlan["allocated"][0]["name"] == "IT-TEMP"
        assert asn["allocated"][0]["asn"] == 64513
        assert cable["path_far_end"] == "lab-sw2 GigabitEthernet1/0/2 (interface)"
        peer = await call(client, "netbox_whats_connected", device="lab-sw2", interface="Gi1/0/2")
        assert peer["far_end"][0]["device"] == "lab-sw1"
    finally:
        await call(client, "netbox_delete_object", object_type="dcim.cables", ids=[cable["cable"]["id"]])
        await call(client, "netbox_delete_object", object_type="ip-addresses", ids=[ip["allocated"][0]["id"]])
        await call(client, "netbox_delete_object", object_type="vlans", ids=[vlan["allocated"][0]["id"]])
        await call(client, "netbox_delete_object", object_type="asns", ids=[asn["allocated"][0]["id"]])


async def test_run_script_dry_run_then_commit(client: Client[Any]) -> None:
    sw2 = (
        await call(
            client,
            "netbox_list_objects",
            object_type="devices",
            filters={"name": "lab-sw2"},
            fields=["id", "description"],
        )
    )["results"][0]
    original = sw2.get("description", "")
    dry = await call(
        client,
        "netbox_run_script",
        script="SetDescription",
        data={"device": sw2["id"], "new_description": "it-dry"},
    )
    assert dry["status"] == "completed" and dry["commit"] is False
    after_dry = await call(
        client, "netbox_get_object", object_type="devices", id=sw2["id"], fields=["description"]
    )
    assert after_dry.get("description", "") == original  # rolled back

    try:
        done = await call(
            client,
            "netbox_run_script",
            script="SetDescription",
            data={"device": sw2["id"], "new_description": "it-commit"},
            commit=True,
        )
        assert done["status"] == "completed"
        after = await call(
            client, "netbox_get_object", object_type="devices", id=sw2["id"], fields=["description"]
        )
        assert after["description"] == "it-commit"
    finally:
        await call(
            client,
            "netbox_update_object",
            object_type="devices",
            id=sw2["id"],
            data={"description": original},
        )
