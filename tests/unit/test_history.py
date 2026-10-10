import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from netbox_mcp.config import Settings
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.schema import build_index
from netbox_mcp.server import create_server
from netbox_mcp.tools.common import content_type
from netbox_mcp.tools.history import diff, format_change, parse_time

from .conftest import MINI_SPEC, preview_then_apply

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def page(*results: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"count": len(results), "next": None, "results": list(results)})


async def call(settings: Settings, tool: str, args: dict[str, Any]) -> Any:
    async with Client(create_server(settings)) as client:
        return (await client.call_tool(tool, args)).structured_content


# --- helpers --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30m", "2026-10-10T11:30:00+00:00"),
        ("24h", "2026-10-09T12:00:00+00:00"),
        ("7d", "2026-10-03T12:00:00+00:00"),
        ("2w", "2026-09-26T12:00:00+00:00"),
        ("2026-10-01", "2026-10-01T00:00:00"),
        (None, None),
    ],
)
def test_parse_time(value: str | None, expected: str | None) -> None:
    assert parse_time(value, now=NOW) == expected


def test_parse_time_rejects_nonsense() -> None:
    with pytest.raises(InvalidRequest, match="24h"):
        parse_time("yesterday")


def test_diff_only_changed_fields_with_nested_custom_fields() -> None:
    before = {
        "name": "sw1",
        "status": "planned",
        "last_updated": "a",
        "custom_fields": {"owner": "x", "rack": 1},
    }
    after = {
        "name": "sw1",
        "status": "active",
        "last_updated": "b",
        "custom_fields": {"owner": "y", "rack": 1},
    }
    assert diff(before, after) == {
        "custom_fields.owner": {"from": "x", "to": "y"},
        "status": {"from": "planned", "to": "active"},
    }


def test_format_change_create_drops_noise() -> None:
    change = {
        "id": 1,
        "time": "t",
        "user_name": "alice",
        "action": {"value": "create", "label": "Created"},
        "changed_object_type": "dcim.device",
        "changed_object_id": 5,
        "object_repr": "sw1",
        "message": "",
        "request_id": "r1",
        "postchange_data": {"name": "sw1", "serial": "", "interface_count": 0, "site": 3, "created": "x"},
    }
    assert format_change(change) == {
        "id": 1,
        "time": "t",
        "user": "alice",
        "action": "create",
        "object_type": "dcim.device",
        "object_id": 5,
        "object": "sw1",
        "request_id": "r1",
        "data": {"name": "sw1", "site": 3},
    }


def test_content_type_mapping() -> None:
    idx = build_index(MINI_SPEC)
    assert content_type(idx.types["dcim.devices"]) == "dcim.device"
    assert content_type(idx.types["virtualization.interfaces"]) == "virtualization.vminterface"
    assert content_type(idx.types["extras.journal-entries"]) == "extras.journalentry"


# --- tools ----------------------------------------------------------------------------------


async def test_change_history_for_object_merges_related_and_labels_refs(
    settings: Settings, netbox: respx.MockRouter
) -> None:
    netbox.get("/dcim/devices/", params={"name": "sw1"}).mock(return_value=page({"id": 5, "name": "sw1"}))
    direct = {
        "id": 10,
        "time": "2026-10-10T10:00:00Z",
        "user_name": "alice",
        "action": {"value": "update"},
        "changed_object_type": "dcim.device",
        "changed_object_id": 5,
        "object_repr": "sw1",
        "request_id": "r1",
        "prechange_data": {"site": 3, "status": "planned", "last_updated": "a"},
        "postchange_data": {"site": 4, "status": "active", "last_updated": "b"},
    }
    related = {
        "id": 11,
        "time": "2026-10-10T11:00:00Z",
        "user_name": "bob",
        "action": {"value": "delete"},
        "changed_object_type": "dcim.interface",
        "changed_object_id": 7,
        "object_repr": "Gi1",
        "request_id": "r2",
        "prechange_data": {"name": "Gi1"},
    }
    changes = netbox.get("/core/object-changes/")
    changes.side_effect = lambda req: page(related) if "related_object_id" in req.url.params else page(direct)
    netbox.get("/dcim/sites/").mock(
        return_value=page(
            {"id": 3, "display": "Old Site", "url": "u"}, {"id": 4, "display": "New Site", "url": "u"}
        )
    )

    out = await call(
        settings, "netbox_change_history", {"object_type": "devices", "object": "sw1", "since": "7d"}
    )

    params = [dict(c.request.url.params) for c in changes.calls]
    assert {p.get("changed_object_id") for p in params} == {"5", None}
    assert {p.get("related_object_type") for p in params} == {"dcim.device", None}
    assert all("time_after" in p for p in params)
    assert out["object"] == "dcim.device sw1 (id 5)"
    assert [c["id"] for c in out["changes"]] == [11, 10]  # newest first
    assert out["changes"][1]["changes"] == {
        "site": {"from": "Old Site (id 3)", "to": "New Site (id 4)"},
        "status": {"from": "planned", "to": "active"},
    }


async def test_change_history_needs_type_for_object(settings: Settings, netbox: respx.MockRouter) -> None:
    async with Client(create_server(settings)) as client:
        with pytest.raises(ToolError, match="needs object_type"):
            await client.call_tool("netbox_change_history", {"object": "sw1"})


async def test_journal_resolves_usernames(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/", params={"name": "sw1"}).mock(return_value=page({"id": 5, "name": "sw1"}))
    journal = netbox.get("/extras/journal-entries/").mock(
        return_value=page(
            {
                "id": 1,
                "created": "t",
                "created_by": 3,
                "kind": {"value": "warning"},
                "assigned_object_type": "dcim.device",
                "assigned_object": {"id": 5, "url": "u", "display": "sw1"},
                "comments": "replaced PSU",
            }
        )
    )
    netbox.get("/users/users/").mock(return_value=page({"id": 3, "username": "alice"}))
    out = await call(settings, "netbox_journal", {"object_type": "devices", "object": "sw1"})
    assert journal.calls.last.request.url.params["assigned_object_type"] == "dcim.device"
    assert out["entries"] == [
        {
            "id": 1,
            "created": "t",
            "by": "alice",
            "kind": "warning",
            "object_type": "dcim.device",
            "object": "sw1",
            "comments": "replaced PSU",
        }
    ]


async def test_add_journal_entry_is_write_gated(settings: Settings, netbox: respx.MockRouter) -> None:
    async with Client(create_server(settings)) as client:
        assert "netbox_add_journal_entry" not in {t.name for t in await client.list_tools()}

    rw = settings.model_copy(update={"netbox_mcp_enable_writes": True})
    netbox.get("/dcim/devices/5/").mock(return_value=httpx.Response(200, json={"id": 5, "name": "sw1"}))
    post = netbox.post("/extras/journal-entries/").mock(
        return_value=httpx.Response(201, json={"id": 9, "created": "t"})
    )
    args = {"object_type": "devices", "object": 5, "comments": "hi"}
    async with Client(create_server(rw)) as client:
        preview, applied = await preview_then_apply(client, "netbox_add_journal_entry", args)
    assert preview["preview"] == {"object": "dcim.device sw1 (id 5)", "kind": "info", "comments": "hi"}
    assert post.call_count == 1
    assert json.loads(post.calls.last.request.content) == {
        "assigned_object_type": "dcim.device",
        "assigned_object_id": 5,
        "kind": "info",
        "comments": "hi",
    }
    assert applied["id"] == 9
