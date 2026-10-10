import json
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from netbox_mcp.config import Settings
from netbox_mcp.server import create_server
from netbox_mcp.tools.automation import job_summary

N = "http://netbox.test"


@pytest.fixture
def rw(settings: Settings) -> Settings:
    return settings.model_copy(update={"netbox_mcp_enable_writes": True})


def page(*results: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"count": len(results), "next": None, "results": list(results)})


async def call(settings: Settings, tool: str, args: dict[str, Any]) -> Any:
    async with Client(create_server(settings)) as client:
        return (await client.call_tool(tool, args)).structured_content


SCRIPT = {
    "id": 1,
    "name": "SetDescription",
    "display": "SetDescription (mcp_lab)",
    "description": "Set a description",
    "vars": {"device": "ObjectVar", "new_description": "StringVar"},
    "is_executable": True,
}


def test_job_summary() -> None:
    job = {
        "id": 7,
        "status": {"value": "completed"},
        "object": {"display": "SetDescription (mcp_lab)"},
        "data": {"output": "dry run", "log": [{"status": "success", "message": "changed"}]},
        "error": "",
    }
    assert job_summary(job) == {
        "job_id": 7,
        "object": "SetDescription (mcp_lab)",
        "status": "completed",
        "output": "dry run",
        "log": ["success: changed"],
    }


async def test_list_scripts(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/extras/scripts/").mock(
        return_value=page({**SCRIPT, "result": {"status": {"value": "completed"}, "completed": "t"}})
    )
    out = await call(settings, "netbox_list_scripts", {})
    assert out["scripts"] == [
        {
            "id": 1,
            "name": "SetDescription",
            "module": "mcp_lab",
            "description": "Set a description",
            "inputs": {"device": "ObjectVar", "new_description": "StringVar"},
            "executable": True,
            "last_run": {"status": "completed", "at": "t"},
        }
    ]


async def test_run_script_dry_run_waits_for_job(rw: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/extras/scripts/").mock(return_value=page(SCRIPT))
    run = netbox.post("/extras/scripts/1/").mock(
        return_value=httpx.Response(200, json={**SCRIPT, "result": {"id": 9, "status": {"value": "pending"}}})
    )
    netbox.get("/core/jobs/9/").mock(
        side_effect=[
            httpx.Response(200, json={"id": 9, "status": {"value": "running"}}),
            httpx.Response(
                200, json={"id": 9, "status": {"value": "completed"}, "data": {"output": "rolled back"}}
            ),
        ]
    )
    out = await call(rw, "netbox_run_script", {"script": "mcp_lab.SetDescription", "data": {"device": 5}})
    assert json.loads(run.calls.last.request.content) == {"data": {"device": 5}, "commit": False}
    assert (out["commit"], out["status"], out["output"]) == (False, "completed", "rolled back")


async def test_run_script_rejects_unknown_inputs_and_is_gated(
    settings: Settings, rw: Settings, netbox: respx.MockRouter
) -> None:
    netbox.get("/extras/scripts/1/").mock(return_value=httpx.Response(200, json=SCRIPT))
    run = netbox.post("/extras/scripts/1/")
    async with Client(create_server(rw)) as client:
        with pytest.raises(ToolError, match=r"Unknown input\(s\) for SetDescription: \['bogus'\]"):
            await client.call_tool("netbox_run_script", {"script": 1, "data": {"bogus": 1}})
    assert not run.called
    async with Client(create_server(settings)) as client:
        assert "netbox_run_script" not in {t.name for t in await client.list_tools()}


async def test_export_lists_and_renders(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/extras/export-templates/").mock(
        return_value=page(
            {"name": "Device CSV", "object_types": ["dcim.device"]},
            {"name": "Prefix CSV", "object_types": ["ipam.prefix"]},
        )
    )
    out = await call(settings, "netbox_export", {"object_type": "devices"})
    assert out["templates"] == [{"name": "Device CSV"}]

    export = netbox.get("/dcim/devices/", params={"export": "Device CSV"}).mock(
        return_value=httpx.Response(
            200, text="name\nsw1\n", headers={"content-type": "text/csv; charset=utf-8"}
        )
    )
    out = await call(
        settings,
        "netbox_export",
        {"object_type": "devices", "template": "Device CSV", "filters": {"status": "active"}},
    )
    assert export.calls.last.request.url.params["status"] == "active"
    assert out == {
        "object_type": "dcim.devices",
        "template": "Device CSV",
        "content_type": "text/csv",
        "lines": 2,
        "content": "name\nsw1\n",
    }


async def test_graphql(settings: Settings, netbox: respx.MockRouter) -> None:
    with respx.mock(base_url=N, assert_all_called=False) as gql_router:
        gql_router.get("/api/status/").mock(return_value=httpx.Response(200, json={}))
        gql = gql_router.post("/graphql/").mock(
            return_value=httpx.Response(200, json={"data": {"device_list": [{"name": "sw1"}]}})
        )
        out = await call(settings, "netbox_graphql", {"query": "{ device_list { name } }"})
    assert json.loads(gql.calls.last.request.content)["query"] == "{ device_list { name } }"
    assert out == {"data": {"device_list": [{"name": "sw1"}]}}

    async with Client(create_server(settings)) as client:
        with pytest.raises(ToolError, match="read-only"):
            await client.call_tool("netbox_graphql", {"query": "# comment\n  Mutation { x }"})
