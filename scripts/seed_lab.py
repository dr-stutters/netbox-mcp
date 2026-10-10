"""Seed (or remove) a small test topology in NetBox for developing and testing netbox-mcp.

Everything created is tagged `mcp-test`. Safe to re-run: existing objects are reused.

    uv run python scripts/seed_lab.py            # create / update
    uv run python scripts/seed_lab.py --delete   # remove everything tagged mcp-test

Topology (site "MCP Lab", location "DC1" with racks R1/R2, VRF "LAB"):

    lab-rtr1 Gi2 ──────────── Gi1/0/1 lab-sw1 Gi1/0/3 ── FP1 lab-pp1 RP1 ══ RP1 lab-pp2 FP1 ── Gi1/0/3 lab-sw2
    lab-rtr1 Gi4 ── circuit MCP-CID-001 (A: MCP Lab) ── (Z: provider network "MCP Telco Backbone")
    lab-rtr1 Gi3 ──────────────────────────────────────────────────────────────────────────── Gi1/0/1 lab-sw2
                               lab-sw1 Po1 (Gi1/0/4) ─────────────────────────────── (Gi1/0/4) Po1 lab-sw2
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings

TAG = "mcp-test"
# Seeded objects that can't carry the mcp-test tag; --delete finds these by name instead.
UNTAGGED = (
    "extras/tags/",
    "extras/config-contexts/",
    "extras/custom-fields/",
    "extras/custom-field-choice-sets/",
)

# Jinja2 template rendered by NetBox; config-context keys (ntp_servers, ...) are top-level variables.
CONFIG_TEMPLATE = """hostname {{ device.name }}
ip domain name {{ domain_name }}
{% for server in ntp_servers %}ntp server {{ server }}
{% endfor %}logging host {{ syslog_server }}
{% if stp_mode %}spanning-tree mode {{ stp_mode }}
{% endif %}!
{% for iface in device.interfaces.all() %}interface {{ iface.name }}
{% if iface.description %} description {{ iface.description }}
{% endif %}{% for ip in iface.ip_addresses.all() %} ip address {{ ip.address.ip }} {{ ip.address.netmask }}
{% endfor %}{% if not iface.enabled %} shutdown
{% endif %}!
{% endfor %}end
"""


class Seeder:
    def __init__(self, nb: NetBoxClient) -> None:
        self.nb = nb
        self.tag_id = 0

    async def ensure(
        self, path: str, lookup: dict[str, Any], data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Get the object matching `lookup`, creating it (lookup + data + tag) if missing."""
        existing, count = await self.nb.list(path, lookup, limit=2)
        if count > 1:
            raise RuntimeError(f"{path} lookup {lookup} matched {count} objects")
        if existing:
            return existing[0]
        body = {**lookup, **(data or {})}
        # Config contexts use `tags` as an assignment rule, not a label; custom fields have no tags.
        if self.tag_id and path not in UNTAGGED:
            body.setdefault("tags", [{"id": self.tag_id}])
        obj: dict[str, Any] = await self.nb.request("POST", path, json=body)
        print(f"  created {path}{obj['id']}  {obj.get('display')}")
        return obj

    async def patch(self, path: str, obj_id: int, data: dict[str, Any]) -> None:
        await self.nb.request("PATCH", f"{path}{obj_id}/", json=data)

    async def seed(self) -> None:
        tag = await self.ensure("extras/tags/", {"slug": TAG}, {"name": TAG, "color": "ff9800"})
        self.tag_id = tag["id"]

        site = await self.ensure("dcim/sites/", {"slug": "mcp-lab"}, {"name": "MCP Lab", "status": "active"})
        cisco = await self.ensure("dcim/manufacturers/", {"slug": "cisco"}, {"name": "Cisco"})
        generic = await self.ensure("dcim/manufacturers/", {"slug": "generic"}, {"name": "Generic"})
        iosxe = await self.ensure(
            "dcim/platforms/", {"slug": "ios-xe"}, {"name": "IOS-XE", "manufacturer": cisco["id"]}
        )
        dt_rtr = await self.ensure(
            "dcim/device-types/", {"slug": "c8000v"}, {"model": "C8000V", "manufacturer": cisco["id"]}
        )
        dt_sw = await self.ensure(
            "dcim/device-types/", {"slug": "c9300-24t"}, {"model": "C9300-24T", "manufacturer": cisco["id"]}
        )
        dt_pp = await self.ensure(
            "dcim/device-types/",
            {"slug": "patch-panel-24"},
            {"model": "Patch Panel 24", "manufacturer": generic["id"]},
        )
        roles = {}
        for slug, name, color in [
            ("router", "Router", "2196f3"),
            ("switch", "Switch", "4caf50"),
            ("patch-panel", "Patch Panel", "9e9e9e"),
            ("access-point", "Access Point", "00bcd4"),
            ("pdu", "PDU", "f44336"),
        ]:
            roles[slug] = await self.ensure(
                "dcim/device-roles/", {"slug": slug}, {"name": name, "color": color}
            )

        def device(name: str, dtype: dict[str, Any], role: str, platform: bool = True) -> dict[str, Any]:
            data = {
                "device_type": dtype["id"],
                "role": roles[role]["id"],
                "site": site["id"],
                "status": "active",
            }
            if platform:
                data["platform"] = iosxe["id"]
            return {"name": name, "data": data}

        devs: dict[str, dict[str, Any]] = {}
        for spec in [
            device("lab-rtr1", dt_rtr, "router"),
            device("lab-sw1", dt_sw, "switch"),
            device("lab-sw2", dt_sw, "switch"),
            device("lab-pp1", dt_pp, "patch-panel", platform=False),
            device("lab-pp2", dt_pp, "patch-panel", platform=False),
        ]:
            devs[spec["name"]] = await self.ensure(
                "dcim/devices/", {"name": spec["name"], "site_id": site["id"]}, spec["data"]
            )

        # --- Location, racks and rack positions ---
        loc = await self.ensure(
            "dcim/locations/",
            {"slug": "mcp-lab-dc1", "site_id": site["id"]},
            {"name": "DC1", "site": site["id"]},
        )
        racks = {}
        for rack_name in ("R1", "R2"):
            racks[rack_name] = await self.ensure(
                "dcim/racks/",
                {"name": rack_name, "site_id": site["id"]},
                {"site": site["id"], "location": loc["id"], "status": "active", "u_height": 42},
            )
        for dev_name, rack_name, position in [
            ("lab-pp1", "R1", 42),
            ("lab-rtr1", "R1", 40),
            ("lab-sw1", "R1", 38),
            ("lab-pp2", "R2", 42),
            ("lab-sw2", "R2", 38),
        ]:
            await self.patch(
                "dcim/devices/",
                devs[dev_name]["id"],
                {
                    "location": loc["id"],
                    "rack": racks[rack_name]["id"],
                    "position": position,
                    "face": "front",
                },
            )
        me = await self.nb.get("authentication-check/")
        await self.ensure(
            "dcim/rack-reservations/",
            {"rack_id": racks["R2"]["id"], "description": "Reserved for lab firewall"},
            {"rack": racks["R2"]["id"], "units": [20, 21], "user": me["id"], "status": "active"},
        )

        # --- IPAM scaffolding ---
        vrf = await self.ensure("ipam/vrfs/", {"name": "LAB"}, {"rd": "65000:99"})
        vgroup = await self.ensure(
            "ipam/vlan-groups/",
            {"slug": "mcp-lab"},
            {"name": "MCP Lab", "scope_type": "dcim.site", "scope_id": site["id"]},
        )
        vlans = {}
        for vid, name in [(10, "USERS"), (20, "SERVERS"), (99, "MGMT")]:
            vlans[vid] = await self.ensure(
                "ipam/vlans/", {"vid": vid, "group_id": vgroup["id"]}, {"name": name, "group": vgroup["id"]}
            )

        scope = {"scope_type": "dcim.site", "scope_id": site["id"]}
        await self.ensure(
            "ipam/prefixes/",
            {"prefix": "10.99.0.0/16", "vrf_id": vrf["id"]},
            {"vrf": vrf["id"], "status": "container", **scope, "description": "MCP Lab supernet"},
        )
        for vid, net in [(10, "10.99.10.0/24"), (20, "10.99.20.0/24"), (99, "10.99.99.0/24")]:
            await self.ensure(
                "ipam/prefixes/",
                {"prefix": net, "vrf_id": vrf["id"]},
                {
                    "vrf": vrf["id"],
                    "vlan": vlans[vid]["id"],
                    "status": "active",
                    **scope,
                    "description": vlans[vid]["name"],
                },
            )
        await self.ensure(
            "ipam/prefixes/",
            {"prefix": "10.99.255.0/24", "vrf_id": vrf["id"]},
            {"vrf": vrf["id"], "status": "container", **scope, "description": "Point-to-point links"},
        )
        await self.ensure(
            "ipam/prefixes/",
            {"prefix": "10.99.255.0/31", "vrf_id": vrf["id"]},
            {"vrf": vrf["id"], "status": "active", **scope, "description": "rtr1-sw2"},
        )

        # A DHCP range inside SERVERS, and a private ASN range with its first ASN in use.
        await self.ensure(
            "ipam/ip-ranges/",
            {"start_address": "10.99.20.100/24", "vrf_id": vrf["id"]},
            {
                "end_address": "10.99.20.150/24",
                "vrf": vrf["id"],
                "status": "active",
                "description": "DHCP pool",
            },
        )
        rir = await self.ensure(
            "ipam/rirs/", {"slug": "mcp-private"}, {"name": "MCP Private", "is_private": True}
        )
        await self.ensure(
            "ipam/asn-ranges/",
            {"slug": "mcp-lab-asns"},
            {"name": "MCP Lab ASNs", "rir": rir["id"], "start": 64512, "end": 64520},
        )
        await self.ensure("ipam/asns/", {"asn": 64512}, {"rir": rir["id"], "description": "MCP Lab core"})

        # --- Interfaces ---
        async def iface(dev: str, name: str, itype: str, **extra: Any) -> dict[str, Any]:
            return await self.ensure(
                "dcim/interfaces/",
                {"device_id": devs[dev]["id"], "name": name},
                {"device": devs[dev]["id"], "type": itype, **extra},
            )

        r_gi1 = await iface("lab-rtr1", "GigabitEthernet1", "1000base-t", description="Management")
        r_gi2 = await iface("lab-rtr1", "GigabitEthernet2", "1000base-t", description="to lab-sw1")
        r_gi3 = await iface("lab-rtr1", "GigabitEthernet3", "1000base-t", description="to lab-sw2")

        sw: dict[str, dict[str, dict[str, Any]]] = {}
        for name in ("lab-sw1", "lab-sw2"):
            po1 = await iface(name, "Port-channel1", "lag", mode="tagged", description="inter-switch trunk")
            ifs = {"Po1": po1}
            ifs["Gi1/0/1"] = await iface(
                name, "GigabitEthernet1/0/1", "1000base-t", mode="tagged", description="to lab-rtr1"
            )
            ifs["Gi1/0/2"] = await iface(
                name,
                "GigabitEthernet1/0/2",
                "1000base-t",
                mode="access",
                untagged_vlan=vlans[10]["id"],
                description="user access port",
            )
            ifs["Gi1/0/3"] = await iface(
                name, "GigabitEthernet1/0/3", "1000base-t", description="via patch panels"
            )
            ifs["Gi1/0/4"] = await iface(
                name, "GigabitEthernet1/0/4", "1000base-t", lag=po1["id"], description="Po1 member"
            )
            ifs["Vlan99"] = await iface(name, "Vlan99", "virtual", description="Management SVI")
            for key in ("Po1", "Gi1/0/1"):
                await self.patch(
                    "dcim/interfaces/", ifs[key]["id"], {"tagged_vlans": [v["id"] for v in vlans.values()]}
                )
            sw[name] = ifs

        # --- Patch panels: one front + rear port each ---
        pp: dict[str, dict[str, dict[str, Any]]] = {}
        for name in ("lab-pp1", "lab-pp2"):
            rp = await self.ensure(
                "dcim/rear-ports/",
                {"device_id": devs[name]["id"], "name": "RP1"},
                {"device": devs[name]["id"], "type": "lc", "positions": 1},
            )
            fp = await self.ensure(
                "dcim/front-ports/",
                {"device_id": devs[name]["id"], "name": "FP1"},
                {
                    "device": devs[name]["id"],
                    "type": "lc",
                    "rear_ports": [{"position": 1, "rear_port": rp["id"], "rear_port_position": 1}],
                },
            )
            pp[name] = {"rp": rp, "fp": fp}

        # --- Cables ---
        async def cable(a: tuple[str, int], b: tuple[str, int], label: str) -> None:
            await self.ensure(
                "dcim/cables/",
                {"label": label},
                {
                    "a_terminations": [{"object_type": a[0], "object_id": a[1]}],
                    "b_terminations": [{"object_type": b[0], "object_id": b[1]}],
                    "status": "connected",
                    "type": "cat6",
                },
            )

        IF, FP, RP = "dcim.interface", "dcim.frontport", "dcim.rearport"
        await cable((IF, r_gi2["id"]), (IF, sw["lab-sw1"]["Gi1/0/1"]["id"]), "MCP-001")
        await cable((IF, r_gi3["id"]), (IF, sw["lab-sw2"]["Gi1/0/1"]["id"]), "MCP-002")
        await cable((IF, sw["lab-sw1"]["Gi1/0/4"]["id"]), (IF, sw["lab-sw2"]["Gi1/0/4"]["id"]), "MCP-003")
        await cable((IF, sw["lab-sw1"]["Gi1/0/3"]["id"]), (FP, pp["lab-pp1"]["fp"]["id"]), "MCP-004")
        await cable((RP, pp["lab-pp1"]["rp"]["id"]), (RP, pp["lab-pp2"]["rp"]["id"]), "MCP-005")
        await cable((FP, pp["lab-pp2"]["fp"]["id"]), (IF, sw["lab-sw2"]["Gi1/0/3"]["id"]), "MCP-006")

        # --- A provider circuit: lab-rtr1 Gi4 -> circuit A side (site) ... Z side (provider network) ---
        provider = await self.ensure("circuits/providers/", {"slug": "mcp-telco"}, {"name": "MCP Telco"})
        ctype = await self.ensure("circuits/circuit-types/", {"slug": "internet"}, {"name": "Internet"})
        backbone = await self.ensure(
            "circuits/provider-networks/",
            {"name": "MCP Telco Backbone", "provider_id": provider["id"]},
            {"provider": provider["id"]},
        )
        circuit = await self.ensure(
            "circuits/circuits/",
            {"cid": "MCP-CID-001"},
            {"provider": provider["id"], "type": ctype["id"], "status": "active", "commit_rate": 100000},
        )
        term_a = await self.ensure(
            "circuits/circuit-terminations/",
            {"circuit_id": circuit["id"], "term_side": "A"},
            {
                "circuit": circuit["id"],
                "termination_type": "dcim.site",
                "termination_id": site["id"],
                "port_speed": 1000000,
                "xconnect_id": "XC-0001",
            },
        )
        await self.ensure(
            "circuits/circuit-terminations/",
            {"circuit_id": circuit["id"], "term_side": "Z"},
            {
                "circuit": circuit["id"],
                "termination_type": "circuits.providernetwork",
                "termination_id": backbone["id"],
            },
        )
        r_gi4 = await iface(
            "lab-rtr1", "GigabitEthernet4", "1000base-t", description="Internet uplink (MCP-CID-001)"
        )
        await cable((IF, r_gi4["id"]), ("circuits.circuittermination", term_a["id"]), "MCP-007")

        # --- IP addresses, MACs, primary IPs ---
        async def ip(address: str, iface_obj: dict[str, Any], **extra: Any) -> dict[str, Any]:
            return await self.ensure(
                "ipam/ip-addresses/",
                {"address": address, "vrf_id": vrf["id"]},
                {
                    "vrf": vrf["id"],
                    "assigned_object_type": IF,
                    "assigned_object_id": iface_obj["id"],
                    "status": "active",
                    **extra,
                },
            )

        mgmt = {
            "lab-rtr1": await ip("10.99.99.1/24", r_gi1, dns_name="lab-rtr1.mcp.lab"),
            "lab-sw1": await ip("10.99.99.11/24", sw["lab-sw1"]["Vlan99"], dns_name="lab-sw1.mcp.lab"),
            "lab-sw2": await ip("10.99.99.12/24", sw["lab-sw2"]["Vlan99"], dns_name="lab-sw2.mcp.lab"),
        }
        await ip("10.99.10.1/24", r_gi2, description="USERS gateway")
        await ip("10.99.255.0/31", r_gi3)
        for name, addr in mgmt.items():
            await self.patch("dcim/devices/", devs[name]["id"], {"primary_ip4": addr["id"]})

        for iface_obj, mac in [
            (r_gi1, "52:54:00:99:00:01"),
            (sw["lab-sw1"]["Vlan99"], "52:54:00:99:00:11"),
            (sw["lab-sw2"]["Vlan99"], "52:54:00:99:00:12"),
        ]:
            m = await self.ensure(
                "dcim/mac-addresses/",
                {"mac_address": mac},
                {"assigned_object_type": IF, "assigned_object_id": iface_obj["id"]},
            )
            await self.patch("dcim/interfaces/", iface_obj["id"], {"primary_mac_address": m["id"]})

        # --- VPN: an IPsec tunnel between lab-rtr1 and lab-sw2 over the 10.99.255.0/31 link ---
        ike_prop = await self.ensure(
            "vpn/ike-proposals/",
            {"name": "MCP IKE AES256"},
            {
                "authentication_method": "preshared-keys",
                "encryption_algorithm": "aes-256-cbc",
                "authentication_algorithm": "hmac-sha256",
                "group": 14,
            },
        )
        ike_pol = await self.ensure(
            "vpn/ike-policies/", {"name": "MCP IKEv2"}, {"version": 2, "proposals": [ike_prop["id"]]}
        )
        esp_prop = await self.ensure(
            "vpn/ipsec-proposals/", {"name": "MCP ESP AES256-GCM"}, {"encryption_algorithm": "aes-256-gcm"}
        )
        esp_pol = await self.ensure(
            "vpn/ipsec-policies/", {"name": "MCP IPsec"}, {"proposals": [esp_prop["id"]], "pfs_group": 14}
        )
        profile = await self.ensure(
            "vpn/ipsec-profiles/",
            {"name": "MCP Site-to-Site"},
            {"mode": "esp", "ike_policy": ike_pol["id"], "ipsec_policy": esp_pol["id"]},
        )
        tunnel = await self.ensure(
            "vpn/tunnels/",
            {"name": "MCP-TUN-1"},
            {
                "status": "active",
                "encapsulation": "ipsec-tunnel",
                "ipsec_profile": profile["id"],
                "tunnel_id": 100,
            },
        )
        p2p_sw2 = await ip("10.99.255.1/31", sw["lab-sw2"]["Gi1/0/1"])
        p2p_rtr, _ = await self.nb.list(
            "ipam/ip-addresses/", {"address": "10.99.255.0/31", "vrf_id": vrf["id"]}, limit=1
        )
        for dev_name, outside in (("lab-rtr1", p2p_rtr[0]), ("lab-sw2", p2p_sw2)):
            tun_if = await iface(dev_name, "Tunnel1", "virtual", description="MCP-TUN-1")
            await self.ensure(
                "vpn/tunnel-terminations/",
                {"tunnel_id": tunnel["id"], "termination_id": tun_if["id"]},
                {
                    "tunnel": tunnel["id"],
                    "role": "peer",
                    "termination_type": IF,
                    "termination_id": tun_if["id"],
                    "outside_ip": outside["id"],
                },
            )

        # --- L2VPN: VXLAN-EVPN stretching VLAN 20 ---
        l2vpn = await self.ensure(
            "vpn/l2vpns/",
            {"slug": "mcp-evpn-servers"},
            {"name": "MCP-EVPN-SERVERS", "type": "vxlan-evpn", "identifier": 10020},
        )
        await self.ensure(
            "vpn/l2vpn-terminations/",
            {"l2vpn_id": l2vpn["id"], "vlan_id": vlans[20]["id"]},
            {
                "l2vpn": l2vpn["id"],
                "assigned_object_type": "ipam.vlan",
                "assigned_object_id": vlans[20]["id"],
            },
        )

        # --- Wireless: an AP on lab-sw1 Gi1/0/5 serving two SSIDs ---
        dt_ap = await self.ensure(
            "dcim/device-types/", {"slug": "ap-6"}, {"model": "AP-6", "manufacturer": generic["id"]}
        )
        ap = await self.ensure(
            "dcim/devices/",
            {"name": "lab-ap1", "site_id": site["id"]},
            {
                "device_type": dt_ap["id"],
                "role": roles["access-point"]["id"],
                "site": site["id"],
                "status": "active",
            },
        )
        devs["lab-ap1"] = ap
        wgroup = await self.ensure(
            "wireless/wireless-lan-groups/", {"slug": "mcp-wireless"}, {"name": "MCP Wireless"}
        )
        corp = await self.ensure(
            "wireless/wireless-lans/",
            {"ssid": "MCP-Corp"},
            {
                "group": wgroup["id"],
                "status": "active",
                "vlan": vlans[10]["id"],
                "auth_type": "wpa-enterprise",
                "auth_cipher": "aes",
            },
        )
        guest = await self.ensure(
            "wireless/wireless-lans/",
            {"ssid": "MCP-Guest"},
            {
                "group": wgroup["id"],
                "status": "active",
                "auth_type": "wpa-personal",
                "auth_cipher": "aes",
                "auth_psk": "mcp-lab-guest-psk",
            },
        )
        await self.patch("wireless/wireless-lans/", guest["id"], {"auth_psk": "mcp-lab-guest-psk"})
        await iface("lab-ap1", "wlan0", "ieee802.11ax", rf_role="ap", wireless_lans=[corp["id"], guest["id"]])
        ap_eth = await iface("lab-ap1", "eth0", "1000base-t", description="uplink")
        sw1_gi5 = await iface(
            "lab-sw1",
            "GigabitEthernet1/0/5",
            "1000base-t",
            mode="access",
            untagged_vlan=vlans[10]["id"],
            description="lab-ap1",
        )
        await cable((IF, sw1_gi5["id"]), (IF, ap_eth["id"]), "MCP-008")

        # --- Power: panel -> feed (rack R1) -> lab-pdu1 -> lab-sw1 / lab-rtr1 PSUs ---
        panel = await self.ensure(
            "dcim/power-panels/",
            {"name": "MCP Panel A", "site_id": site["id"]},
            {"site": site["id"], "location": loc["id"]},
        )
        feed = await self.ensure(
            "dcim/power-feeds/",
            {"name": "MCP Feed A1", "power_panel_id": panel["id"]},
            {
                "power_panel": panel["id"],
                "rack": racks["R1"]["id"],
                "status": "active",
                "supply": "ac",
                "phase": "single-phase",
                "voltage": 230,
                "amperage": 16,
                "max_utilization": 80,
            },
        )
        dt_pdu = await self.ensure(
            "dcim/device-types/", {"slug": "pdu-8"}, {"model": "PDU-8", "manufacturer": generic["id"]}
        )
        pdu = await self.ensure(
            "dcim/devices/",
            {"name": "lab-pdu1", "site_id": site["id"]},
            {"device_type": dt_pdu["id"], "role": roles["pdu"]["id"], "site": site["id"], "status": "active"},
        )
        await self.patch(
            "dcim/devices/",
            pdu["id"],
            {"location": loc["id"], "rack": racks["R1"]["id"], "position": 1, "face": "front"},
        )

        async def component(path: str, device: dict[str, Any], name: str, **data: Any) -> dict[str, Any]:
            return await self.ensure(
                path, {"device_id": device["id"], "name": name}, {"device": device["id"], **data}
            )

        pdu_in = await component("dcim/power-ports/", pdu, "PSU-IN", type="iec-60320-c20", maximum_draw=3680)
        outlets = [
            await component(
                "dcim/power-outlets/",
                pdu,
                f"Outlet{n}",
                type="iec-60320-c13",
                power_port=pdu_in["id"],
                feed_leg="A",
            )
            for n in (1, 2)
        ]
        PP, PO, PF = "dcim.powerport", "dcim.poweroutlet", "dcim.powerfeed"
        await cable((PF, feed["id"]), (PP, pdu_in["id"]), "MCP-PWR-001")
        for (dev_name, max_w, alloc_w), outlet, lbl in zip(
            (("lab-sw1", 350, 120), ("lab-rtr1", 250, 90)),
            outlets,
            ("MCP-PWR-002", "MCP-PWR-003"),
            strict=True,
        ):
            psu = await component(
                "dcim/power-ports/",
                devs[dev_name],
                "PSU1",
                type="iec-60320-c14",
                maximum_draw=max_w,
                allocated_draw=alloc_w,
            )
            await cable((PP, psu["id"]), (PO, outlet["id"]), lbl)

        # --- Config template (via the IOS-XE platform) and config contexts ---
        template = await self.ensure(
            "extras/config-templates/", {"name": "MCP IOS-XE base"}, {"template_code": CONFIG_TEMPLATE}
        )
        await self.patch("dcim/platforms/", iosxe["id"], {"config_template": template["id"]})
        await self.ensure(
            "extras/config-contexts/",
            {"name": "MCP Lab defaults"},
            {
                "weight": 1000,
                "sites": [site["id"]],
                "data": {
                    "ntp_servers": ["10.99.99.250", "10.99.99.251"],
                    "syslog_server": "10.99.99.252",
                    "domain_name": "mcp.lab",
                },
            },
        )
        await self.ensure(
            "extras/config-contexts/",
            {"name": "MCP Lab switches"},
            {
                "weight": 2000,
                "sites": [site["id"]],
                "roles": [roles["switch"]["id"]],
                "data": {"ntp_servers": ["10.99.99.253"], "stp_mode": "rapid-pvst"},
            },
        )

        # --- A select custom field on devices, set on lab-sw1 ---
        tiers = await self.ensure(
            "extras/custom-field-choice-sets/",
            {"name": "MCP Tiers"},
            {"extra_choices": [["gold", "Gold"], ["silver", "Silver"]]},
        )
        await self.ensure(
            "extras/custom-fields/",
            {"name": "mcp_tier"},
            {
                "label": "MCP tier",
                "type": "select",
                "object_types": ["dcim.device"],
                "choice_set": tiers["id"],
            },
        )
        await self.patch("dcim/devices/", devs["lab-sw1"]["id"], {"custom_fields": {"mcp_tier": "gold"}})

        print("Seed complete.")

    async def delete(self) -> None:
        # Dependency order: things that reference others first.
        # Untagged objects (see UNTAGGED), found by their MCP name prefix.
        for path, by_name in (
            ("extras/config-contexts/", {"name__isw": "MCP Lab "}),
            ("extras/custom-fields/", {"name__isw": "mcp_"}),
            ("extras/custom-field-choice-sets/", {"name__isw": "MCP "}),
        ):
            objs, _ = await self.nb.list(path, by_name, limit=100)
            if objs:
                await self.nb.request("DELETE", path, json=[{"id": o["id"]} for o in objs])
                print(f"  deleted {len(objs)} from {path}")
        order = [
            "dcim/cables/",
            "vpn/tunnel-terminations/",
            "vpn/tunnels/",
            "vpn/ipsec-profiles/",
            "vpn/ipsec-policies/",
            "vpn/ipsec-proposals/",
            "vpn/ike-policies/",
            "vpn/ike-proposals/",
            "vpn/l2vpn-terminations/",
            "vpn/l2vpns/",
            "wireless/wireless-lans/",
            "wireless/wireless-lan-groups/",
            "dcim/mac-addresses/",
            "ipam/ip-addresses/",
            "dcim/front-ports/",
            "dcim/rear-ports/",
            "dcim/interfaces/",
            "dcim/devices/",
            "dcim/power-feeds/",
            "dcim/power-panels/",
            "circuits/circuit-terminations/",
            "circuits/circuits/",
            "circuits/provider-networks/",
            "circuits/circuit-types/",
            "circuits/providers/",
            "dcim/rack-reservations/",
            "dcim/racks/",
            "dcim/locations/",
            "ipam/prefixes/",
            "ipam/vlans/",
            "ipam/vlan-groups/",
            "ipam/ip-ranges/",
            "ipam/vrfs/",
            "ipam/asns/",
            "ipam/asn-ranges/",
            "ipam/rirs/",
            "dcim/device-types/",
            "dcim/platforms/",
            "extras/config-templates/",
            "dcim/manufacturers/",
            "dcim/device-roles/",
            "dcim/sites/",
        ]
        for path in order:
            objs, _ = await self.nb.list(path, {"tag": TAG, "brief": "true"}, limit=1000)
            if objs:
                await self.nb.request("DELETE", path, json=[{"id": o["id"]} for o in objs])
                print(f"  deleted {len(objs)} from {path}")
        print("Delete complete (the mcp-test tag itself is kept).")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--delete", action="store_true", help="remove all objects tagged mcp-test")
    args = parser.parse_args()
    settings = Settings()  # type: ignore[call-arg]
    async with NetBoxClient(
        settings.netbox_url, settings.netbox_token.get_secret_value(), verify=settings.verify
    ) as nb:
        seeder = Seeder(nb)
        await (seeder.delete() if args.delete else seeder.seed())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001 - CLI: print and exit non-zero
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
