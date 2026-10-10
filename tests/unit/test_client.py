import httpx
import pytest
import respx

from netbox_mcp.client import NetBoxClient, auth_header
from netbox_mcp.errors import NetBoxError

BASE = "http://netbox.test"


def make_client() -> NetBoxClient:
    return NetBoxClient(BASE, "nbt_abc.def")


def test_auth_header_detects_token_version() -> None:
    assert auth_header("nbt_abc.def") == "Bearer nbt_abc.def"
    assert auth_header("0123456789abcdef") == "Token 0123456789abcdef"


@respx.mock
async def test_get_sends_auth_and_parses_json() -> None:
    route = respx.get(f"{BASE}/api/status/").mock(
        return_value=httpx.Response(200, json={"netbox-version": "4.7.2"})
    )
    async with make_client() as nb:
        assert await nb.status() == {"netbox-version": "4.7.2"}
    assert route.calls.last.request.headers["Authorization"] == "Bearer nbt_abc.def"


@respx.mock
async def test_path_normalisation() -> None:
    respx.get(f"{BASE}/api/dcim/sites/").mock(return_value=httpx.Response(200, json={"ok": 1}))
    async with make_client() as nb:
        assert await nb.get("/api/dcim/sites/") == {"ok": 1}
        assert await nb.get("dcim/sites/") == {"ok": 1}


@respx.mock
async def test_list_follows_pages_up_to_limit() -> None:
    def page(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        limit = int(request.url.params["limit"])
        items = [{"id": i} for i in range(offset, min(offset + limit, 600))]
        nxt = "x" if offset + limit < 600 else None
        return httpx.Response(200, json={"count": 600, "next": nxt, "results": items})

    respx.get(f"{BASE}/api/ipam/ip-addresses/").mock(side_effect=page)
    async with make_client() as nb:
        results, count = await nb.list("ipam/ip-addresses/", limit=300)
    assert count == 600
    assert [r["id"] for r in results] == list(range(300))


@respx.mock
async def test_validation_error_is_readable() -> None:
    respx.post(f"{BASE}/api/dcim/sites/").mock(
        return_value=httpx.Response(400, json={"slug": ["This field is required."]})
    )
    async with make_client() as nb:
        with pytest.raises(NetBoxError, match="slug: This field is required") as exc:
            await nb.request("POST", "dcim/sites/", json={"name": "x"})
    assert exc.value.status_code == 400


@respx.mock
async def test_permission_error_hints_at_token() -> None:
    respx.get(f"{BASE}/api/dcim/sites/").mock(return_value=httpx.Response(403, json={"detail": "nope"}))
    async with make_client() as nb:
        with pytest.raises(NetBoxError, match="NETBOX_TOKEN"):
            await nb.get("dcim/sites/")


@respx.mock
async def test_get_retries_on_503(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("netbox_mcp.client.asyncio.sleep", no_sleep)
    route = respx.get(f"{BASE}/api/status/").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
    )
    async with make_client() as nb:
        assert await nb.status() == {"ok": True}
    assert route.call_count == 2


@respx.mock
async def test_post_is_not_retried() -> None:
    route = respx.post(f"{BASE}/api/dcim/sites/").mock(return_value=httpx.Response(503))
    async with make_client() as nb:
        with pytest.raises(NetBoxError):
            await nb.request("POST", "dcim/sites/", json={})
    assert route.call_count == 1


@respx.mock
async def test_connection_error_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("netbox_mcp.client.MAX_RETRIES", 0)
    respx.get(f"{BASE}/api/status/").mock(side_effect=httpx.ConnectError("boom"))
    async with make_client() as nb:
        with pytest.raises(NetBoxError, match="Could not connect"):
            await nb.status()
