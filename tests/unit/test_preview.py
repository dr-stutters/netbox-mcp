import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client

from netbox_mcp.config import Settings
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.preview import PreviewStore
from netbox_mcp.server import create_server

from .conftest import preview_then_apply

ARGS = {"object_type": "devices", "id": 5, "data": {"serial": "A"}}


def test_token_is_single_use() -> None:
    store = PreviewStore()
    token = store.issue("tool", ARGS, ["state"], "alice")
    store.redeem(token, "tool", ARGS, ["state"], "alice")
    with pytest.raises(InvalidRequest, match="Unknown or expired"):
        store.redeem(token, "tool", ARGS, ["state"], "alice")


@pytest.mark.parametrize(
    ("tool", "args", "state", "caller", "reason"),
    [
        ("tool", ARGS, ["state"], "bob", "different user"),
        ("other_tool", ARGS, ["state"], "alice", "issued for tool"),
        ("tool", {**ARGS, "id": 6}, ["state"], "alice", "arguments differ"),
        ("tool", ARGS, ["changed"], "alice", "changed since the preview"),
    ],
)
def test_token_refusals(tool: str, args: dict[str, Any], state: list[str], caller: str, reason: str) -> None:
    store = PreviewStore()
    token = store.issue("tool", ARGS, ["state"], "alice")
    with pytest.raises(InvalidRequest, match=reason):
        store.redeem(token, tool, args, state, caller)


def test_token_expires() -> None:
    store = PreviewStore(ttl=-1)
    token = store.issue("tool", ARGS, None, "alice")
    with pytest.raises(InvalidRequest, match="Unknown or expired"):
        store.redeem(token, "tool", ARGS, None, "alice")


def page(*results: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"count": len(results), "next": None, "results": list(results)})


async def test_run_script_commit_previews_with_dry_run(settings: Settings, netbox: respx.MockRouter) -> None:
    rw = settings.model_copy(update={"netbox_mcp_enable_writes": True})
    netbox.get("/extras/scripts/").mock(
        return_value=page(
            {"id": 1, "name": "Fix", "display": "Fix (m)", "vars": {"x": "StringVar"}, "is_executable": True}
        )
    )
    run = netbox.post("/extras/scripts/1/").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json={
                "result": {
                    "id": 10 if not json.loads(req.content)["commit"] else 11,
                    "status": {"value": "completed"},
                    "data": {"output": "dry" if not json.loads(req.content)["commit"] else "done"},
                }
            },
        )
    )
    async with Client(create_server(rw)) as client:
        preview, applied = await preview_then_apply(
            client, "netbox_run_script", {"script": "Fix", "data": {"x": "1"}, "commit": True}
        )
        assert preview["preview"]["dry_run"]["output"] == "dry"
        assert (applied["output"], applied["result_status"]) == ("done", "completed")
        assert [json.loads(c.request.content)["commit"] for c in run.calls] == [False, True]

        # commit=false is itself a dry run: it runs straight away, no token needed.
        direct = (
            await client.call_tool("netbox_run_script", {"script": "Fix", "data": {"x": "1"}})
        ).structured_content
        assert direct["commit"] is False and "confirm" not in direct
