from typing import Any

import httpx
import respx
from fastmcp import Client

from netbox_mcp.config import Settings
from netbox_mcp.server import create_server
from netbox_mcp.tools.hygiene import ALL_CHECKS, analyse_prefixes, find_duplicate_ips

LAB = {"id": 1, "display": "LAB"}
OTHER = {"id": 2, "display": "OTHER"}


def ip(id_: int, address: str, vrf: dict[str, Any] | None = None, role: str | None = None) -> dict[str, Any]:
    return {"id": id_, "address": address, "vrf": vrf, "role": {"value": role} if role else None}


def pfx(
    id_: int, prefix: str, status: str = "active", vrf: dict[str, Any] | None = None, **kw: Any
) -> dict[str, Any]:
    return {"id": id_, "prefix": prefix, "status": {"value": status}, "vrf": vrf, **kw}


def page(*results: dict[str, Any], count: int | None = None) -> httpx.Response:
    return httpx.Response(
        200, json={"count": len(results) if count is None else count, "next": None, "results": list(results)}
    )


def test_duplicate_ips_per_vrf_ignoring_shared_roles() -> None:
    ips = [
        ip(1, "10.0.0.1/24"),
        ip(2, "10.0.0.1/32"),  # same host, different mask: still a duplicate
        ip(3, "10.0.0.1/24", LAB),  # same host, other VRF: fine
        ip(4, "10.0.0.254/24", role="vrrp"),
        ip(5, "10.0.0.254/24", role="vrrp"),  # shared VIP: fine
    ]
    assert find_duplicate_ips(ips) == ["10.0.0.1 (global): ids 1, 2"]


def test_analyse_prefixes() -> None:
    prefixes = [
        pfx(1, "10.0.0.0/16", "container"),
        pfx(2, "10.0.0.0/30"),  # 2 usable, both used -> 100%
        pfx(3, "10.0.1.0/24"),  # no IPs, no children -> unused
        pfx(4, "10.0.2.0/24", mark_utilized=True),  # skipped entirely
        pfx(5, "10.0.128.0/17", "reserved", LAB),  # child of the global container from another VRF
    ]
    ips = [ip(1, "10.0.0.1/30"), ip(2, "10.0.0.2/30"), ip(3, "192.168.1.1/24"), ip(4, "10.0.0.1/30", LAB)]
    out = analyse_prefixes(prefixes, ips, threshold=90)
    assert out["ips_outside_prefixes"] == ["192.168.1.1/24 (global)", "10.0.0.1/30 (LAB)"]
    assert out["prefixes_over_threshold"] == ["10.0.0.0/30 (global): 100% used"]
    assert out["prefixes_unused"] == ["10.0.1.0/24 (global)"]

    # The global container counts every child, including LAB's /17: well over 50%.
    assert (
        "10.0.0.0/16 (global): 51% used"
        in analyse_prefixes(prefixes, ips, threshold=50)["prefixes_over_threshold"]
    )


async def test_hygiene_tool(settings: Settings, netbox: respx.MockRouter) -> None:
    devices = netbox.get("/dcim/devices/")

    def device_response(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        if p.get("has_primary_ip") == "false":
            return page({"id": 1, "name": "sw9", "site": {"display": "Lab"}})
        if p.get("position__empty") == "true":
            return page({"id": 2, "name": "pdu1", "rack": {"display": "R1"}}, {"id": 3, "name": "loose"})
        return page()

    devices.side_effect = device_response
    trunks = netbox.get("/dcim/interfaces/", params={"cabled": "false"}).mock(
        return_value=page({"name": "Gi1/0/48", "device": {"display": "sw9"}})
    )
    netbox.get("/dcim/interfaces/").mock(return_value=page())
    netbox.get("/virtualization/interfaces/").mock(return_value=page())
    netbox.get("/ipam/prefixes/").mock(return_value=page(pfx(1, "10.0.0.0/24", vlan={"id": 10})))
    netbox.get("/ipam/ip-addresses/").mock(return_value=page(ip(1, "10.0.0.1/24"), ip(2, "10.0.0.1/24")))
    netbox.get("/ipam/vlans/").mock(
        return_value=page({"id": 10, "vid": 10, "name": "USERS"}, {"id": 20, "vid": 20, "name": "OLD"})
    )

    async with Client(create_server(settings)) as client:
        out = (await client.call_tool("netbox_data_hygiene", {})).structured_content

    assert out is not None
    assert out["checks_run"] == len(ALL_CHECKS)
    problems = out["problems"]
    assert problems["duplicate_ips"]["examples"] == ["10.0.0.1 (global): ids 1, 2"]
    assert problems["devices_without_primary_ip"]["examples"] == ["sw9 (Lab)"]
    assert problems["devices_unpositioned_in_rack"] == {
        "description": "Devices assigned to a rack but without a unit position",
        "count": 1,
        "examples": ["pdu1 (rack R1)"],
    }
    assert problems["vlans_unused"]["examples"] == ["20 OLD (global)"]  # VLAN 10 has a prefix
    assert problems["trunks_without_cable"]["examples"] == ["sw9 Gi1/0/48"]
    params = trunks.calls.last.request.url.params
    assert params.get_list("mode") == ["tagged", "tagged-all"]
    assert (params["kind"], params["enabled"], params["mark_connected"]) == ("physical", "true", "false")
    assert "ips_outside_prefixes" in out["passed"]
    assert out["issues_total"] == sum(p["count"] for p in problems.values())


async def test_hygiene_selected_checks_only(settings: Settings, netbox: respx.MockRouter) -> None:
    devices = netbox.get("/dcim/devices/").mock(return_value=page(count=0))
    prefixes = netbox.get("/ipam/prefixes/")
    async with Client(create_server(settings)) as client:
        out = (
            await client.call_tool("netbox_data_hygiene", {"checks": ["devices_without_serial"]})
        ).structured_content
    assert out == {
        "scope": "all sites",
        "checks_run": 1,
        "issues_total": 0,
        "problems": {},
        "passed": ["devices_without_serial"],
    }
    assert devices.calls.last.request.url.params["serial__empty"] == "true"
    assert not prefixes.called  # IP/prefix data is only fetched when an IP check runs
