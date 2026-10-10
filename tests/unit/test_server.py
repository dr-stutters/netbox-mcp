import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from netbox_mcp.config import Settings
from netbox_mcp.server import create_server

BASE = "http://netbox.test"


def settings() -> Settings:
    return Settings(_env_file=None, netbox_url=BASE, netbox_token="nbt_x.y")  # type: ignore[call-arg]


@respx.mock
async def test_status_tool() -> None:
    respx.get(f"{BASE}/api/status/").mock(
        return_value=httpx.Response(
            200, json={"netbox-version": "4.7.2", "netbox-full-version": "4.7.2-Docker", "plugins": {}}
        )
    )
    respx.get(f"{BASE}/api/authentication-check/").mock(
        return_value=httpx.Response(200, json={"username": "netbox-mcp"})
    )
    async with Client(create_server(settings())) as client:
        result = await client.call_tool("netbox_status")
    assert result.structured_content == {
        "netbox_url": BASE,
        "netbox_version": "4.7.2-Docker",
        "plugins": {},
        "rq_workers_running": None,
        "api_user": "netbox-mcp",
        "writes_enabled": False,
    }


@respx.mock
async def test_netbox_errors_reach_the_model() -> None:
    respx.get(f"{BASE}/api/status/").mock(return_value=httpx.Response(403, json={"detail": "Invalid token"}))
    async with Client(create_server(settings())) as client:
        with pytest.raises(ToolError, match="Invalid token"):
            await client.call_tool("netbox_status")
