# netbox-mcp

An [MCP](https://modelcontextprotocol.io) server that lets Claude, or any MCP client, work with
[NetBox](https://netboxlabs.com/oss/netbox/), the network source of truth. Ask questions in plain
language and the model uses NetBox's API for you:

> *"Where is 10.99.99.11?"* · *"Summarise lab-sw1"* · *"Trace the cable from lab-sw1 Gi1/0/3"* ·
> *"What's on VLAN 10?"* · *"What changed on lab-sw1 this week?"* · *"Where can a 2U device go in DC1?"* ·
> *"What's wrong with the data at site X?"* · *"Give me the next free /24 in 10.99.0.0/16 and reserve it for the lab"*

Built with [FastMCP](https://gofastmcp.com) for **NetBox 4.x** (tested on 4.7).

## Features

- **Task-shaped tools**: IP and MAC lookup, device and interface summaries, end-to-end cable traces
  through patch panels, prefix utilisation, next free IPs and prefixes, and VLAN usage.
- **Works with every object type**: generic list, get, create, update and delete for all of
  NetBox's ~140 object types, plus tools that let the model explore NetBox's own API schema.
- **Catches silent mistakes**: NetBox ignores misspelled filter and field names, which can return
  every object or quietly drop data. This server checks them against the schema first and replies
  with "did you mean …?".
- **Read-only by default**: write tools exist only when you turn them on.
- **Small responses**: compact JSON with empty fields and links removed, to save the model's context.
- **Runs two ways**: locally over stdio (Claude Code), or as a shared HTTP service in Docker with
  per-person bearer tokens and a log of who called what.

## Contents

- [Requirements](#requirements)
- [1. Create a NetBox API token](#1-create-a-netbox-api-token)
- [2a. Install locally for Claude Code](#2a-install-locally-for-claude-code-stdio)
- [2b. Run as a shared server with Docker](#2b-run-as-a-shared-server-with-docker)
- [2c. Put it on the internet with Caddy, for Claude Desktop / claude.ai](#2c-put-it-on-the-internet-with-caddy-for-claude-desktop--claudeai)
- [Configuration reference](#configuration-reference)
- [Tools](#tools)
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Requirements

- A **NetBox 4.x** instance that's reachable from wherever this server runs.
- **For local use:** [uv](https://docs.astral.sh/uv/getting-started/installation/). uv installs
  Python 3.12+ for you if needed.
- **For a shared server:** Docker with the Compose plugin.

## 1. Create a NetBox API token

Using a dedicated service account is recommended, so that changes made through the MCP server are
clearly labelled in NetBox's changelog.

1. **Admin → Authentication → Users → Add.** Create a user such as `netbox-mcp`. Leave staff and
   superuser unticked.
2. **Admin → Authentication → Permissions → Add.** Select all object types and tick **View**.
   Assign the permission to `netbox-mcp`. If you'll enable writes, also tick
   **Add / Change / Delete**.
3. **Admin → API Tokens → Add.** Pick the `netbox-mcp` user. Tick **Write enabled** only if you'll
   enable writes. Copy the token straight away, because NetBox shows it only once.

Both v2 tokens (`nbt_…`, NetBox 4.5+) and older v1 tokens work. Paste the token on its own, without
a `Bearer` or `Token` prefix.

## 2a. Install locally for Claude Code (stdio)

```bash
git clone https://github.com/dr-stutters/netbox-mcp.git
cd netbox-mcp
uv sync
cp .env.example .env
```

Edit `.env` and set at least:

```ini
NETBOX_URL=https://netbox.example.com
NETBOX_TOKEN=nbt_xxxxxxxx.yyyyyyyy
# NETBOX_MCP_ENABLE_WRITES=true   # optional: allow changes
```

Register the server with Claude Code. Use the full path to the folder you cloned:

```bash
claude mcp add netbox -s user -- uv --directory /full/path/to/netbox-mcp run netbox-mcp
```

Start a new Claude Code session and ask *"check the NetBox connection"*. The model calls
`netbox_status`, which reports the NetBox version, the API user, and whether writes are enabled.

> `uv --directory` runs the server from the project folder, so it reads that folder's `.env`. Your
> NetBox token stays in `.env` and is never stored in Claude's config.

## 2b. Run as a shared server with Docker

The container serves MCP over HTTP at `/mcp`, protected by bearer tokens, plus an unauthenticated
`/health` check. **It won't start without at least one token.**

```bash
git clone https://github.com/dr-stutters/netbox-mcp.git
cd netbox-mcp
cp .env.example .env
```

Edit `.env` and set `NETBOX_URL`, `NETBOX_TOKEN` and, optionally, `NETBOX_MCP_ENABLE_WRITES=true`
and `MCP_PUBLISH_PORT` (the host port, default `8000`).

**Create a token for each person.** Each run prints a raw token and a hashed entry:

```bash
docker compose run --rm --no-deps netbox-mcp netbox-mcp token alice
```

- Give the raw token to Alice. It isn't stored anywhere.
- Add the printed `alice:sha256:…` line to `MCP_AUTH_TOKENS` in `.env`. Separate several people
  with commas: `MCP_AUTH_TOKENS=alice:sha256:…,bob:sha256:…`.

Because `.env` holds only hashes, a leaked `.env` doesn't expose usable tokens.

**Start it:**

```bash
docker compose up -d --build
docker compose ps         # wait for "(healthy)"
docker compose logs -f    # every tool call is logged as: tool=… caller=alice
```

**Connect Claude Code to it:**

```bash
claude mcp add --transport http netbox-remote http://<docker-host>:8000/mcp \
  --header "Authorization: Bearer <token>"
```

**Managing access:**
- **Add someone:** generate a token, add their entry to `MCP_AUTH_TOKENS`, then run
  `docker compose up -d`.
- **Revoke someone:** delete their entry, then run `docker compose up -d`.
- **Update the server:** run `git pull && docker compose up -d --build`.

> If your NetBox uses an internal CA, mount the CA file into the container (for example add
> `- ./ca.pem:/certs/ca.pem:ro` under `volumes:`) and set `NETBOX_CA_BUNDLE=/certs/ca.pem`.

Plain HTTP is fine on a trusted LAN or VPN. For anything else, use the HTTPS setup below.

## 2c. Put it on the internet with Caddy, for Claude Desktop / claude.ai

Claude Desktop and claude.ai **custom connectors connect from Anthropic's cloud**, not from your
computer. The server therefore needs a public HTTPS address, which a reverse proxy such as
[Caddy](https://caddyserver.com) provides, with automatic certificates.

1. On the VPS, follow [2b](#2b-run-as-a-shared-server-with-docker), but keep the port private to
   the VPS so only Caddy can reach it:

   ```ini
   MCP_PUBLISH_PORT=127.0.0.1:8000
   ```

   The VPS must be able to reach `NETBOX_URL`. A NetBox on a private home network needs a VPN, such
   as WireGuard or Tailscale.

2. Add a site to your Caddyfile, then reload Caddy:

   ```caddyfile
   netbox-mcp.example.com {
       reverse_proxy 127.0.0.1:8000
   }
   ```

   If Caddy itself runs in Docker, attach both containers to a shared Docker network and use
   `reverse_proxy netbox-mcp:8000` instead.

3. Check that it's reachable. The first command should print `{"status":"ok"}` and the second `401`:

   ```bash
   curl https://netbox-mcp.example.com/health
   curl -s -o /dev/null -w '%{http_code}\n' -X POST https://netbox-mcp.example.com/mcp
   ```

4. In Claude, go to **Settings → Connectors → Add custom connector**:
   - **URL:** `https://netbox-mcp.example.com/mcp`
   - **Request headers:** `Authorization` = `Bearer <token>`

   Request-header authentication for custom connectors was rolled out gradually. If you don't see
   a Request headers section, update the Claude app.

## Configuration reference

All settings are environment variables. They can go in `.env`, or in Docker as `environment:` or
`env_file:` entries.

| Variable | Default | Description |
|---|---|---|
| `NETBOX_URL` | **required** | NetBox base URL, `http://` or `https://`, without `/api` |
| `NETBOX_TOKEN` | **required** | NetBox API token |
| `NETBOX_VERIFY_SSL` | `true` | Set `false` to skip TLS verification (labs only) |
| `NETBOX_CA_BUNDLE` | — | Path to a CA certificate file for an internal CA |
| `NETBOX_TIMEOUT` | `30` | NetBox request timeout, in seconds |
| `NETBOX_MCP_ENABLE_WRITES` | `false` | Turns on the create, update, delete and allocate tools |
| `NETBOX_MCP_CACHE_DIR` | `~/.cache/netbox-mcp` | Where the schema index is cached (`/cache` in Docker). Empty turns caching off |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http` (the Docker image uses `http`) |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | HTTP bind address and port inside the container |
| `MCP_PATH` | `/mcp` | HTTP endpoint path |
| `MCP_AUTH_TOKENS` | — | Bearer tokens as `name:sha256:<hash>` or `name:<raw>`, comma-separated. Required for HTTP |
| `MCP_AUTH_TOKENS_FILE` | — | The same format, one entry per line, read from a file (e.g. a Docker secret) |
| `MCP_PUBLISH_PORT` | `8000` | Host port Docker Compose publishes (`127.0.0.1:8000` = local only) |
| `LOG_LEVEL` | `INFO` | Logging level |

## Tools

### Everyday tasks

| Tool | What it does |
|---|---|
| `netbox_status` | Connection check: NetBox version, API user, whether writes are enabled |
| `netbox_search` | Search devices, VMs, interfaces, IPs, prefixes, VLANs, sites, racks, cables, circuits… at once |
| `netbox_lookup_ip` | IP → its device or VM and interface, containing prefixes, VLAN and VRF. Works even if the IP isn't recorded |
| `netbox_lookup_mac` | MAC in any format → its interface, device or VM, and IPs |
| `netbox_device_summary` | Device or VM overview: every interface with its IPs, VLANs, LAG, MAC, cable and far-end peer |
| `netbox_interface_detail` | One interface in depth. Short names like `Gi1/0/1` or `Po1` work |
| `netbox_whats_connected` | What's plugged into an interface: the far-end device (through patch panels) with its role, rack position and primary IP, or the circuit/provider network |
| `netbox_trace_cable` | Full cable path through patch panels and circuits, and whether it's complete. Also traces from patch-panel front/rear ports and circuit terminations (by circuit ID), listing every path through them |
| `netbox_prefix_utilization` | Utilisation, using NetBox's own rules, plus child prefixes and the first free IP or free blocks |
| `netbox_next_available_ips` | Preview the next free IPs in a prefix or an IP range (by any address inside it, or `start-end`) |
| `netbox_next_available_prefixes` | Preview free blocks, or the next N child prefixes of a given size |
| `netbox_next_available_vlans` | Preview the next free VLAN IDs in a VLAN group |
| `netbox_next_available_asns` | Preview the next free AS numbers in an ASN range |
| `netbox_vlan_lookup` | VLAN by ID or name, with its prefixes and the tagged and untagged interfaces carrying it |
| `netbox_site_summary` | Everything at a site: details, locations, racks with utilisation, devices by role and status, VMs, prefixes, VLANs and circuits |
| `netbox_rack_elevation` | What's in a rack, unit by unit: devices and faces, reservations, free ranges, utilisation |
| `netbox_find_rack_space` | Racks with room for a device of N units (respects reservations, full or half depth) |
| `netbox_data_hygiene` | Finds data problems: duplicate IPs, IPs outside any prefix, nearly full or unused prefixes, devices without a primary IP or serial, rack devices without a position, unused VLANs, trunks without cables, cabled interfaces without descriptions. Optionally per site |
| `netbox_render_config` | A device's or VM's intended config, rendered from its config template with its config context |
| `netbox_config_context` | A device's or VM's merged config context, and which contexts contributed (in merge order, with what they matched on) |
| `netbox_custom_fields` | Which custom fields exist, their types, choices and object types, and how to filter on them |
| `netbox_circuit_summary` | A circuit end to end: provider, type, commit rate, and each side's termination, cross-connect and the device/interface cabled to it |
| `netbox_tunnel_summary` | A VPN tunnel's endpoints (interface, role, outside IP) and IPsec profile with IKE/IPsec proposals; or list all tunnels |
| `netbox_l2vpn_summary` | An L2VPN (by name, slug or identifier/VNI) with its VLAN, interface and VM-interface members; or list all |
| `netbox_wireless_lookup` | SSIDs with VLAN, security type and the APs serving them |
| `netbox_power_trace` | A device's power chain: PSU → PDU outlet → PDU inlet → feed → panel, plus the rack's power budget |
| `netbox_change_history` | Who changed what and when: filter by object (including its interfaces), user, action and time (`since="7d"`). Updates show only the fields that changed, with references shown by name |
| `netbox_journal` | Journal notes on an object, or the latest notes across NetBox |

### Any object type

| Tool | What it does |
|---|---|
| `netbox_list_object_types` | The ~140 object types the generic tools accept |
| `netbox_list_objects` | List and filter any type. Filter names are checked, including custom-field filters (`cf_<name>`) |
| `netbox_get_object` | One object by type and ID |
| `netbox_search_spec` | Keyword search over NetBox's API spec, to find endpoints |
| `netbox_get_definition` | A type's fields, choice values, required fields and valid filters |
| `netbox_api_call` | Any REST endpoint. GET only unless writes are enabled |

### Changes (only with `NETBOX_MCP_ENABLE_WRITES=true`)

| Tool | What it does |
|---|---|
| `netbox_create_object` | Create one or many objects. Field names are checked |
| `netbox_update_object` | Partial update of one object, or the same change across many |
| `netbox_delete_object` | Delete by ID. If any ID is missing, nothing is deleted |
| `netbox_allocate_ips` | Reserve the next free IP(s) in a prefix or IP range atomically. Optionally assign to an interface and set as primary |
| `netbox_allocate_prefix` | Carve the next free child prefix(es) atomically |
| `netbox_allocate_vlans` | Create VLANs with the next free VIDs in a group (one per name) |
| `netbox_allocate_asns` | Reserve the next free AS number(s) from an ASN range |
| `netbox_connect_cable` | Connect two ports by name, e.g. lab-sw1 Gi1/0/5 → lab-pp1 FP2. Works with interfaces, front/rear ports, console, power and circuit terminations. Refuses ports that are already cabled, and reports where the new path leads |
| `netbox_add_journal_entry` | Add a dated note to any object, e.g. "replaced PSU" (info / success / warning / danger) |

## Security notes

- **Read-only unless you opt in.** When writes are off, the write tools aren't registered at all.
  The NetBox token's own permissions are a second safeguard, so give it only what it needs.
- **HTTP requires tokens.** The server refuses to start in HTTP mode without them. Tokens are
  checked in constant time and can be stored as SHA-256 hashes.
- **Audit trail.** Every tool call is logged with the caller's token name, and writes are also
  logged to the `netbox_mcp.audit` logger. In NetBox's changelog, changes appear under the service
  account's name.
- **Shared credential.** Everyone uses the same NetBox service account. If you need per-user NetBox
  permissions, run separate instances with different NetBox tokens.
- **Secrets are masked.** NetBox stores Wi-Fi PSKs (`auth_psk`) and IKE pre-shared keys
  (`preshared_key`) in plain text. Tool output shows `********` instead, and the summaries only report
  whether one is set. `netbox_api_call` with `compact=false` returns raw data.
- **The container is locked down:** non-root user, read-only filesystem, no Linux capabilities, and
  no `.env` baked into the image.
- **Use HTTPS beyond a trusted network**, both for the MCP endpoint (via Caddy) and, ideally, for
  `NETBOX_URL`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `NetBox denied access (403)` | The token is wrong or expired, or the user lacks permission for that object type |
| `Could not connect … TLS verification failed` | Set `NETBOX_CA_BUNDLE`, or `NETBOX_VERIFY_SSL=false` in a lab |
| `HTTP transport requires MCP_AUTH_TOKENS` | Generate a token (`netbox-mcp token <name>`) and add the entry to `.env` |
| Client gets `401 Unauthorized` | The header must be exactly `Authorization: Bearer <raw token>`, not the `sha256:` entry |
| `Unknown filter` / `Unknown field` | Working as intended: NetBox would silently ignore it. Use the suggested name, or check `netbox_get_definition` |
| `Invalid custom field filter(s)` | NetBox ignores unsupported custom-field lookups (e.g. `cf_<text field>__ic`) and returns everything. Use the form shown in the error or in `netbox_custom_fields`. Loose text fields already match partial text with plain `cf_<name>=part` |
| Write tools missing | Set `NETBOX_MCP_ENABLE_WRITES=true` and restart. For Claude Code, start a new session |
| Container not `(healthy)` | Run `docker compose logs` and look for configuration errors at startup |

## Development

```bash
uv sync
uv run pytest            # unit tests (NetBox is mocked)
uv run ruff check .      # lint
uv run mypy              # strict type checks
```

Integration tests run against a real NetBox seeded with a small lab topology: site "MCP Lab",
routers, switches, patch panels, cables, VLANs and prefixes, all tagged `mcp-test`.

```bash
uv run python scripts/seed_lab.py            # create the lab (safe to re-run)
uv run pytest tests/integration              # read-only checks
NETBOX_MCP_TEST_WRITES=1 uv run pytest tests/integration   # also create/delete test objects
uv run python scripts/seed_lab.py --delete   # remove everything tagged mcp-test
```

Project layout:

```
src/netbox_mcp/
  __init__.py      CLI entry point (server, `token` subcommand)
  config.py        settings from env / .env
  client.py        async NetBox REST client (auth, paging, retries, errors)
  schema.py        OpenAPI schema index: type resolution, filter/field checks, spec search
  auth.py          bearer-token verifier for HTTP
  server.py        FastMCP app, health route, call logging
  formatting.py    compact JSON and Markdown output
  tools/           search, devices, cabling, ipam, generic, spec, writes
```

See [PLAN.md](PLAN.md) for the design notes and roadmap.
