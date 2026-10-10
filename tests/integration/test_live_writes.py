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
