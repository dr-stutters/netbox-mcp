from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from netbox_mcp.config import Settings

BASE = "http://netbox.test"
API = f"{BASE}/api"


def _filters(*names: str) -> list[dict[str, Any]]:
    params = [{"in": "query", "name": n, "schema": {"type": "string"}} for n in names]
    # Pad so list endpoints look like real NetBox list operations (lots of filter params).
    params += [{"in": "query", "name": f"pad{i}", "schema": {"type": "string"}} for i in range(10)]
    return params


def _list_endpoint(model: str, desc: str, filters: list[str], writable: str | None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "get": {
            "operationId": f"{model.lower()}_list",
            "description": f"Get a list of {desc} objects.",
            "parameters": _filters("limit", "offset", "q", *filters),
            "responses": {
                "200": {
                    "content": {
                        "application/json": {"schema": {"$ref": f"#/components/schemas/Paginated{model}List"}}
                    }
                }
            },
        }
    }
    if writable:
        item["post"] = {
            "operationId": f"{model.lower()}_create",
            "requestBody": {
                "content": {
                    "application/json": {
                        "schema": {
                            "oneOf": [
                                {"$ref": f"#/components/schemas/{writable}"},
                                {"type": "array", "items": {"$ref": f"#/components/schemas/{writable}"}},
                            ]
                        }
                    }
                }
            },
            "responses": {"201": {}},
        }
    return item


MINI_SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "NetBox REST API", "version": "4.7.2-test"},
    "paths": {
        "/api/status/": {"get": {"operationId": "status_retrieve", "responses": {"200": {}}}},
        "/api/dcim/devices/": _list_endpoint(
            "Device",
            "device",
            ["name", "name__ic", "status", "status__n", "site", "site_id"],
            "WritableDeviceRequest",
        ),
        "/api/dcim/interfaces/": _list_endpoint("Interface", "interface", ["device_id", "name"], None),
        "/api/dcim/interfaces/{id}/trace/": {
            "get": {
                "operationId": "dcim_interfaces_trace_retrieve",
                "description": "Trace a complete cable path.",
            }
        },
        "/api/virtualization/interfaces/": _list_endpoint(
            "VMInterface", "interface", ["virtual_machine_id"], None
        ),
        "/api/virtualization/virtual-machines/": _list_endpoint(
            "VirtualMachine", "virtual machine", ["status"], None
        ),
        "/api/ipam/prefixes/": _list_endpoint("Prefix", "prefix", ["prefix", "vrf_id"], None),
        "/api/dcim/sites/": _list_endpoint("Site", "site", ["name", "slug"], None),
        "/api/dcim/locations/": _list_endpoint("Location", "location", ["name", "slug", "site_id"], None),
        "/api/dcim/racks/": _list_endpoint("Rack", "rack", ["name", "site_id", "location_id"], None),
        "/api/ipam/vlan-groups/": _list_endpoint("VLANGroup", "VLAN group", ["name", "slug"], None),
        "/api/ipam/asn-ranges/": _list_endpoint("ASNRange", "ASN range", ["name", "slug"], None),
        "/api/virtualization/clusters/": _list_endpoint("Cluster", "cluster", ["name"], None),
        "/api/tenancy/tenants/": _list_endpoint("Tenant", "tenant", ["name", "slug"], None),
        "/api/circuits/circuits/": _list_endpoint("Circuit", "circuit", ["cid"], None),
        "/api/core/object-changes/": _list_endpoint(
            "ObjectChange",
            "object change",
            [
                "changed_object_type",
                "changed_object_id",
                "related_object_type",
                "related_object_id",
                "user_name",
            ],
            None,
        ),
        "/api/extras/journal-entries/": _list_endpoint(
            "JournalEntry",
            "journal entry",
            ["assigned_object_type", "assigned_object_id", "kind"],
            "WritableJournalEntryRequest",
        ),
    },
    "components": {
        "schemas": {
            "Device": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "readOnly": True},
                    "name": {"type": "string", "nullable": True, "maxLength": 64},
                    "status": {
                        "type": "object",
                        "properties": {"value": {"enum": ["active", "offline"]}, "label": {"type": "string"}},
                    },
                    "site": {"$ref": "#/components/schemas/BriefSite"},
                },
            },
            "WritableDeviceRequest": {
                "type": "object",
                "required": ["site"],
                "properties": {
                    "name": {"type": "string", "maxLength": 64},
                    "status": {"enum": ["active", "offline"], "type": "string"},
                    "site": {
                        "oneOf": [{"type": "integer"}, {"$ref": "#/components/schemas/BriefSiteRequest"}]
                    },
                    "tags": {"type": "array", "items": {"$ref": "#/components/schemas/NestedTagRequest"}},
                },
            },
            "BriefSite": {"type": "object", "properties": {"id": {"type": "integer"}}},
            "Interface": {"type": "object", "properties": {}},
            "VMInterface": {"type": "object", "properties": {}},
            "VirtualMachine": {"type": "object", "properties": {}},
            "Prefix": {"type": "object", "properties": {}},
            "Site": {"type": "object", "properties": {"id": {"type": "integer"}}},
            "Location": {"type": "object", "properties": {}},
            "Rack": {"type": "object", "properties": {}},
            "VLANGroup": {"type": "object", "properties": {}},
            "ASNRange": {"type": "object", "properties": {}},
            "Cluster": {"type": "object", "properties": {}},
            "Tenant": {"type": "object", "properties": {}},
            "Circuit": {"type": "object", "properties": {}},
            "ObjectChange": {"type": "object", "properties": {}},
            "JournalEntry": {"type": "object", "properties": {}},
            "WritableJournalEntryRequest": {
                "type": "object",
                "properties": {
                    "assigned_object_type": {"type": "string"},
                    "assigned_object_id": {"type": "integer"},
                    "kind": {"enum": ["info", "warning"], "type": "string"},
                    "comments": {"type": "string"},
                },
            },
        }
    },
}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None, netbox_url=BASE, netbox_token="nbt_x.y", netbox_mcp_cache_dir=tmp_path / "cache"
    )


@pytest.fixture
def netbox() -> Iterator[respx.MockRouter]:
    """respx router with status, auth-check and schema endpoints mocked."""
    with respx.mock(base_url=API, assert_all_called=False) as router:
        router.get("/status/").mock(
            return_value=httpx.Response(
                200, json={"netbox-version": "4.7.2", "netbox-full-version": "4.7.2-test"}
            )
        )
        router.get("/authentication-check/").mock(
            return_value=httpx.Response(200, json={"username": "netbox-mcp"})
        )
        router.get("/schema/").mock(return_value=httpx.Response(200, json=MINI_SPEC))
        yield router
