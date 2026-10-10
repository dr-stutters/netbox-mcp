from typing import Any

import httpx
import respx
from fastmcp import Client

from netbox_mcp.config import Settings
from netbox_mcp.server import create_server


def page(*results: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"count": len(results), "next": None, "results": list(results)})


async def call(settings: Settings, tool: str, args: dict[str, Any]) -> Any:
    async with Client(create_server(settings)) as client:
        return (await client.call_tool(tool, args)).structured_content


async def test_cluster_summary(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/virtualization/clusters/").mock(
        return_value=page(
            {
                "id": 2,
                "name": "lab",
                "type": {"display": "KVM"},
                "status": {"value": "active"},
                "allocated_vcpus": 6.0,
                "allocated_memory": 12288,
                "allocated_disk": 120000,
            }
        )
    )
    netbox.get("/dcim/devices/").mock(
        return_value=page({"id": 1, "name": "hv1", "status": {"value": "active"}})
    )
    vms = netbox.get("/virtualization/virtual-machines/").mock(
        return_value=page(
            {
                "name": "vm2",
                "status": {"value": "planned"},
                "device": {"display": "hv1"},
                "vcpus": 4.0,
                "memory": 8192,
            },
            {
                "name": "vm1",
                "status": {"value": "active"},
                "device": {"display": "hv1"},
                "vcpus": 2.0,
                "memory": 4096,
            },
            {"name": "vm3", "status": {"value": "active"}},
        )
    )
    out = await call(settings, "netbox_cluster_summary", {"cluster": "lab"})
    assert vms.calls.last.request.url.params["cluster_id"] == "2"
    assert out["hosts"] == ["hv1 (active)"]
    assert out["vms_by_status"] == {"active": 2, "planned": 1}
    assert out["vms_per_host"] == {"hv1": 2, "(no host)": 1}
    assert out["allocated"] == {"vcpus": 6.0, "memory_mb": 12288, "disk_mb": 120000}
    assert [v["name"] for v in out["vms"]] == ["vm1", "vm2", "vm3"]


async def test_service_lookup_by_port_and_host(settings: Settings, netbox: respx.MockRouter) -> None:
    services = netbox.get("/ipam/services/").mock(
        return_value=page(
            {
                "name": "HTTPS",
                "protocol": {"value": "tcp"},
                "port_mappings": ["tcp/443"],
                "parent_object_type": "virtualization.virtualmachine",
                "parent": {"display": "web1"},
                "ipaddresses": [{"display": "10.0.0.5/24"}],
            }
        )
    )
    out = await call(settings, "netbox_service_lookup", {"port": 443})
    assert services.calls.last.request.url.params["port"] == "443"
    assert out["services"] == [
        {
            "name": "HTTPS",
            "protocol": "tcp",
            "ports": ["tcp/443"],
            "host": "web1 (vm)",
            "ip_addresses": ["10.0.0.5/24"],
        }
    ]

    netbox.get("/dcim/devices/").mock(return_value=page())
    netbox.get("/virtualization/virtual-machines/").mock(return_value=page({"id": 7, "name": "web1"}))
    await call(settings, "netbox_service_lookup", {"host": "web1"})
    assert services.calls.last.request.url.params["virtual_machine_id"] == "7"


async def test_tenant_summary(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/tenancy/tenants/").mock(
        return_value=page(
            {"id": 1, "name": "Corp", "slug": "corp", "device_count": 2, "vlan_count": 1, "site_count": 0}
        )
    )
    netbox.get("/dcim/devices/").mock(
        return_value=page({"id": 1, "display": "ap1"}, {"id": 2, "display": "ap2"})
    )
    netbox.get("/ipam/vlans/").mock(return_value=page({"id": 1, "display": "USERS (10)"}))
    sites = netbox.get("/dcim/sites/")
    netbox.get("/tenancy/contact-assignments/").mock(return_value=page())
    out = await call(settings, "netbox_tenant_summary", {"tenant": "Corp"})
    assert out["counts"] == {"devices": 2, "vlans": 1}
    assert out["examples"] == {"devices": ["ap1", "ap2"], "vlans": ["USERS (10)"]}
    assert not sites.called  # types the tenant owns nothing of aren't queried


async def test_contacts_for_with_inheritance(settings: Settings, netbox: respx.MockRouter) -> None:
    netbox.get("/dcim/devices/").mock(
        return_value=page({"id": 5, "name": "sw1", "site": {"id": 3, "display": "Lab"}, "tenant": None})
    )
    assignments = netbox.get("/tenancy/contact-assignments/")

    def by_object(request: httpx.Request) -> httpx.Response:
        if request.url.params["object_type"] == "dcim.site":
            return page(
                {
                    "contact": {"id": 1, "display": "NOC"},
                    "role": {"display": "NOC"},
                    "priority": {"value": "primary"},
                }
            )
        return page(
            {
                "contact": {"id": 2, "display": "Alex"},
                "role": {"display": "Owner"},
                "priority": {"value": "secondary"},
            }
        )

    assignments.side_effect = by_object
    netbox.get("/tenancy/contacts/").mock(
        side_effect=lambda req: page(
            {"id": 1, "email": "noc@x", "phone": "123"}
            if req.url.params["id"] == "1"
            else {"id": 2, "email": "alex@x"}
        )
    )
    out = await call(settings, "netbox_contacts_for", {"object_type": "devices", "object": "sw1"})
    assert out["contacts"] == [
        {"name": "Alex", "role": "Owner", "priority": "secondary", "email": "alex@x"},
        {
            "name": "NOC",
            "role": "NOC",
            "priority": "primary",
            "email": "noc@x",
            "phone": "123",
            "via": "site Lab",
        },
    ]
    assert {c.request.url.params["object_type"] for c in assignments.calls} == {"dcim.device", "dcim.site"}
