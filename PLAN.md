# NetBox MCP — Roadmap

Phase 1 (done) delivered the core server. Phase 2 adds dedicated tools for the parts of NetBox's
API that are currently reachable only through the generic tools. Items are built **one at a time**:
each one gets its own branch and PR, with unit tests, live integration tests against the seeded lab
(`scripts/seed_lab.py`), and README updates.

## Conventions for every item

- **Tool design:** task-shaped names (`netbox_<verb>_<thing>`), human-friendly inputs (names, CIDRs,
  abbreviations) with ids as an alternative, compact JSON output, and clear errors with
  "did you mean" hints.
- **Writes:** registered only when `NETBOX_MCP_ENABLE_WRITES=true`. Field names are checked against
  the schema, and every change is logged with the caller's name.
- **Seed data:** if an item needs data the lab doesn't have (racks, circuits, …), extend
  `scripts/seed_lab.py`. Everything stays tagged `mcp-test`.
- **Definition of done:** unit tests + integration tests pass, `ruff` and `mypy --strict` are clean,
  the README tool tables are updated, the PR is merged, and the container on cortex is rebuilt.

---

## Phase 1 — done ✅

| # | Milestone | Result |
|---|---|---|
| 1 | Skeleton | uv project, config, async NetBox client (v1/v2 tokens, HTTP/HTTPS, retries, errors) |
| 2 | Generic + schema tools | list/get any type, schema-validated filters, spec search, definitions, `api_call` |
| 3 | Curated reads | search, IP/MAC lookup, device/interface summary, cable trace, IPAM helpers, VLAN lookup |
| 4 | Writes | env-gated create/update/delete, IP/prefix allocation, schema-validated fields |
| 5 | HTTP + Docker | bearer-token auth (named, hashed), `/health`, hardened container, deployed on cortex:8031 |
| — | Docs | README user guide (local, Docker, Caddy/VPS, config reference) |

---

## Phase 2 — new tools (in order)

### 2.1 Change history & journal ✅

Who changed what and when, plus the ability to leave notes on objects.

| Tool | API | Notes |
|---|---|---|
| `netbox_change_history` | `core/object-changes/` | Filter by object (type + name/id), user, action (create/update/delete) and time window (`since="7d"` or ISO dates). For updates, return **only the fields that changed** (diff of `prechange_data` / `postchange_data`), not whole snapshots. Include the change `message` and `request_id` so changes made together can be grouped. |
| `netbox_journal` | `extras/journal-entries/` (GET) | Read the journal entries for an object |
| `netbox_add_journal_entry` *(write)* | `extras/journal-entries/` (POST) | Add a note (`info` / `success` / `warning` / `danger`) to any object, referenced by type + name/id |

**Done when:** "what changed on lab-sw1 in the last day?" returns field-level diffs, and a journal
entry can be added to and read back from a device.

### 2.2 Site summary & rack space ✅

| Tool | API | Notes |
|---|---|---|
| `netbox_site_summary` | sites, locations, racks, devices, prefixes, VLANs, circuit terminations | One call: site details, device counts by role and status, racks with utilisation, prefixes and VLANs scoped to the site, circuits landing there |
| `netbox_rack_elevation` | `dcim/racks/{id}/elevation/` | Unit-by-unit occupancy (front/rear), with free ranges summarised |
| `netbox_find_rack_space` | racks + elevation | "Where can a 2U device go?" across a site or location, returning racks and free unit positions |

**Seed:** add a location, two racks, rack positions for lab devices, and a reservation.

### 2.3 Complete cabling

| Tool | API | Notes |
|---|---|---|
| `netbox_whats_connected` | `dcim/connected-device/` | Given a device + interface, the device on the far end (the LLDP-style question) |
| `netbox_trace_cable` (extend) | `front-ports/{id}/paths/`, `rear-ports/{id}/paths/`, `circuit-terminations/{id}/paths/` | Trace from patch panel ports and circuit terminations too, listing every path through them |

### 2.4 Data hygiene report

| Tool | API | Notes |
|---|---|---|
| `netbox_data_hygiene` | IPs, prefixes, devices, interfaces, VLANs, cables | Configurable checks, optionally scoped to a site or tenant: duplicate IPs (per VRF), active devices without a primary IP, IPs not inside any prefix, prefixes over a utilisation threshold, VLANs with no prefixes or interfaces, enabled uplinks without cables, devices with no site position or serial, interfaces without descriptions. Returns counts plus the first N offenders per check. |

### 2.5 More allocation & cabling writes

| Tool | API | Notes |
|---|---|---|
| `netbox_next_available_vlans` / `netbox_allocate_vlan` *(write)* | `ipam/vlan-groups/{id}/available-vlans/` | Peek at and reserve the next free VID in a group |
| `netbox_next_available_asns` / `netbox_allocate_asn` *(write)* | `ipam/asn-ranges/{id}/available-asns/` | The same for ASNs |
| IP-range support in IP tools | `ipam/ip-ranges/{id}/available-ips/` | `netbox_next_available_ips` / `netbox_allocate_ips` accept a range as well as a prefix |
| `netbox_connect_cable` *(write)* | `dcim/cables/` | Connect by names: "lab-sw1 Gi1/0/5 → lab-pp1 FP2". Resolves port types, refuses ports that are already cabled, and sets label/type/colour/length |

### 2.6 Config rendering & custom fields

| Tool | API | Notes |
|---|---|---|
| `netbox_render_config` | `devices/{id}/render-config/`, `virtual-machines/{id}/render-config/` | Intended config from the assigned config template |
| `netbox_config_context` | device/VM `config_context` | The merged config context for a device or VM, and which contexts contributed to it |
| `netbox_custom_fields` | `extras/custom-fields/`, `custom-field-choice-sets/` | Which custom fields exist, what object types they apply to, their types and choices, so the model can filter on them (`cf_<name>`) |

### 2.7 Domain summaries: circuits, VPN, wireless, power

| Tool | API | Notes |
|---|---|---|
| `netbox_circuit_summary` | circuits, terminations, providers, paths | Provider/account, type, commit rate, both ends (site or provider network) and the device + interface at each end |
| `netbox_tunnel_summary` | `vpn/tunnels/`, terminations, IPsec profiles | Endpoints, interfaces, outside IPs, crypto profile |
| `netbox_l2vpn_summary` | `vpn/l2vpns/`, terminations | Type, identifier, member VLANs and interfaces across sites |
| `netbox_wireless_lookup` | `wireless/wireless-lans/`, `wireless-links/` | SSID → group, VLAN, auth type, and the interfaces/APs serving it |
| `netbox_power_trace` | power ports/outlets/feeds/panels `trace/` | Device PSU → PDU outlet → feed → panel, plus per-rack power budget |

**Seed:** a provider and circuit, a tunnel, a wireless LAN, and a power panel/feed/PDU chain.

### 2.8 Virtualization, services & tenancy

| Tool | API | Notes |
|---|---|---|
| `netbox_cluster_summary` | clusters, VMs, virtual disks, devices | Hosts, VMs by status, total vCPU/RAM/disk, VMs per host |
| `netbox_service_lookup` | `ipam/services/` | "What listens on port 443 / what runs on host X", with protocol, ports and IPs |
| `netbox_tenant_summary` | tenants + scoped objects | Everything a tenant owns, by type |
| `netbox_contacts_for` | `tenancy/contact-assignments/` | Who to contact for a device/site/circuit, by role and priority |

### 2.9 Composite analysis

| Tool | API | Notes |
|---|---|---|
| `netbox_impact_analysis` | cables, traces, VMs, circuits, VLANs | "What's affected if X goes down?": directly connected devices, VMs on a host, circuits terminating on it, VLANs/prefixes that lose an uplink |
| `netbox_topology` | cables + traces | Neighbour graph for a device (N hops) or a site, as a Mermaid diagram plus an edge list |
| `netbox_compare` | any two objects | Field-by-field differences between two devices/interfaces/VLANs, e.g. two switches in a pair |

### 2.10 Automation hooks

| Tool | API | Notes |
|---|---|---|
| `netbox_list_scripts` / `netbox_run_script` *(write)* | `extras/scripts/` | Discover custom scripts and their inputs, run one (with optional commit=false dry run) |
| `netbox_job_status` | `core/jobs/` | Status and output of script/sync jobs |
| `netbox_export` | export templates (`?export=`) | Run an export template for a filtered set (CSV, text) |
| `netbox_graphql` | `/graphql/` | Read-only GraphQL for complex nested queries |

---

## Platform backlog (scheduled between items as needed)

| Item | Why |
|---|---|
| CI (lint, types, tests, Docker build) + publish the image to GHCR | Catches regressions on every PR; the VPS can pull an image instead of cloning a private repo and building |
| Per-token roles (`name:ro:…` / `name:rw:…`) | Share the hosted server without giving everyone write access |
| Write preview (`confirm=true` to apply) | Show the exact change before applying it on shared servers |
| Multiple NetBox instances in one server | One endpoint for the home NetBox and the VPS NetBox |
| Prometheus metrics, rate limiting | Operations visibility and protection for shared servers |
| MCP prompts (e.g. "audit site", "add access switch") | Ready-made workflows in clients that support prompts |
| OAuth sign-in | A fallback if a client can't send bearer headers |
