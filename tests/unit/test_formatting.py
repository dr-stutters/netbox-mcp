import json

import pytest

from netbox_mcp import formatting
from netbox_mcp.formatting import compact, list_result

DEVICE = {
    "id": 1,
    "url": "http://nb/api/dcim/devices/1/",
    "display_url": "http://nb/dcim/devices/1/",
    "display": "core1",
    "name": "core1",
    "status": {"value": "active", "label": "Active"},
    "site": {
        "id": 2,
        "url": "http://nb/api/dcim/sites/2/",
        "display": "Home",
        "name": "Home",
        "slug": "home",
    },
    "tenant": None,
    "serial": "",
    "tags": [],
    "custom_fields": {"owner": None},
    "interface_count": 0,
    "console_port_count": 2,
    "created": "2026-01-01T00:00:00Z",
}


def test_compact_object() -> None:
    assert compact(DEVICE) == {
        "id": 1,
        "name": "core1",
        "status": "active",
        "site": {"id": 2, "display": "Home"},
        "console_port_count": 2,
        "created": "2026-01-01T00:00:00Z",
    }


def test_compact_in_list_drops_timestamps() -> None:
    assert "created" not in compact(DEVICE, in_list=True)


def test_compact_keeps_generic_relation_context() -> None:
    ip = {
        "id": 5,
        "display": "10.0.0.1/24",
        "address": "10.0.0.1/24",
        "assigned_object": {
            "id": 9,
            "url": "u",
            "display": "eth0",
            "device": {"id": 1, "url": "u", "display": "core1"},
        },
    }
    assert compact(ip) == {
        "id": 5,
        "address": "10.0.0.1/24",
        "assigned_object": {"id": 9, "display": "eth0", "device": {"id": 1, "display": "core1"}},
    }


def test_list_result_pagination_meta() -> None:
    res = list_result([{"id": 1}, {"id": 2}], count=10, offset=4)
    assert res.structured_content == {
        "count": 10,
        "offset": 4,
        "returned": 2,
        "next_offset": 6,
        "results": [{"id": 1}, {"id": 2}],
    }


def test_list_result_truncates_to_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(formatting, "MAX_RESPONSE_CHARS", 500)
    rows = [{"id": i, "name": "x" * 50} for i in range(50)]
    res = list_result(rows, count=50, offset=0)
    payload = res.structured_content
    assert payload is not None
    assert payload["returned"] < 50
    assert payload["next_offset"] == payload["returned"]
    assert "truncated" in payload["note"]
    assert len(json.dumps(payload["results"], separators=(",", ":"))) <= 500


def test_markdown_table() -> None:
    res = list_result([DEVICE], count=1, offset=0, fmt="markdown")
    text = res.content[0].text  # type: ignore[union-attr]
    assert "| id | name | status | site |" in text
    assert "| 1 | core1 | active | Home |" in text
