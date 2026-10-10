from pathlib import Path

import httpx
import pytest
import respx

from netbox_mcp.client import NetBoxClient
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.schema import SchemaStore, build_index

from .conftest import API, BASE, MINI_SPEC

INDEX = build_index(MINI_SPEC)


def test_indexes_model_list_endpoints_only() -> None:
    assert set(INDEX.types) == {
        "dcim.devices",
        "dcim.interfaces",
        "virtualization.interfaces",
        "virtualization.virtual-machines",
        "ipam.prefixes",
    }
    dev = INDEX.types["dcim.devices"]
    assert (dev.model, dev.writable, dev.path) == ("Device", "WritableDeviceRequest", "dcim/devices/")
    assert dev.filters["name"] == ["ic"]
    assert INDEX.types["dcim.interfaces"].actions == ["trace/"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("dcim.devices", "dcim.devices"),
        ("dcim/devices", "dcim.devices"),
        ("/api/dcim/devices/", "dcim.devices"),
        ("devices", "dcim.devices"),
        ("device", "dcim.devices"),
        ("vm", "virtualization.virtual-machines"),
        ("virtual_machines", "virtualization.virtual-machines"),
        ("prefix", "ipam.prefixes"),
    ],
)
def test_resolve_type(name: str, expected: str) -> None:
    assert INDEX.resolve_type(name).key == expected


def test_resolve_type_ambiguous_and_unknown() -> None:
    with pytest.raises(InvalidRequest, match="ambiguous: dcim.interfaces, virtualization.interfaces"):
        INDEX.resolve_type("interfaces")
    with pytest.raises(InvalidRequest, match="Did you mean: dcim.devices"):
        INDEX.resolve_type("dcim.devcies")


def test_check_filters() -> None:
    dev = INDEX.types["dcim.devices"]
    INDEX.check_filters(dev, ["name__ic", "status__n", "site_id", "q", "ordering"])
    with pytest.raises(InvalidRequest, match=r"'stauts' \(did you mean status"):
        INDEX.check_filters(dev, ["stauts"])


def test_definition_for_object_type() -> None:
    d = INDEX.definition("devices")
    assert d["fields"]["status"] == "enum: active|offline"
    assert d["fields"]["site"] == "Site"
    assert d["writable_fields"]["site"] == "Site id (int)"
    assert d["required_for_create"] == ["site"]
    assert "name" in d["filters"]
    assert "filters" not in INDEX.definition("devices", section="write")


def test_definition_for_path_and_schema() -> None:
    d = INDEX.definition("/api/dcim/interfaces/{id}/trace/")
    assert d["operations"][0]["operation_id"] == "dcim_interfaces_trace_retrieve"
    assert INDEX.definition("WritableDeviceRequest")["required"] == ["site"]


def test_search() -> None:
    hits = INDEX.search("trace")
    assert hits[0]["name"] == "GET /api/dcim/interfaces/{id}/trace/"
    assert INDEX.search("virtual machine")[0] == {
        "kind": "object_type",
        "name": "virtualization.virtual-machines",
        "description": "virtual machine objects",
    }


@respx.mock
async def test_store_caches_on_disk(tmp_path: Path) -> None:
    respx.get(f"{API}/status/").mock(
        return_value=httpx.Response(200, json={"netbox-full-version": "4.7.2-test"})
    )
    schema_route = respx.get(f"{API}/schema/").mock(return_value=httpx.Response(200, json=MINI_SPEC))
    async with NetBoxClient(BASE, "t") as nb:
        first = await SchemaStore(nb, tmp_path).get()
        again = await SchemaStore(nb, tmp_path).get()  # new store, same disk cache
    assert schema_route.call_count == 1
    assert set(first.types) == set(again.types)
    assert len(list(tmp_path.glob("schema-index-*.json"))) == 1
