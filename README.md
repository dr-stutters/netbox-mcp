# netbox-mcp

A [FastMCP](https://gofastmcp.com) server for NetBox 4.x. See [PLAN.md](PLAN.md) for the design and roadmap.

## Setup

```bash
uv sync
cp .env.example .env   # then set NETBOX_URL and NETBOX_TOKEN
```

## Use with Claude Code (stdio)

```bash
claude mcp add netbox -s user -- uv --directory /path/to/Netbox-MCP run netbox-mcp
```

`uv --directory` runs the server from the project folder, so it picks up `.env`.

## Run as a shared HTTP server (Docker)

The image serves Streamable HTTP at `/mcp` (bearer-token auth) and an unauthenticated liveness
check at `/health`. It refuses to start in HTTP mode without at least one token.

1. **Configure** — copy `.env.example` to `.env` and set `NETBOX_URL`, `NETBOX_TOKEN` and, if wanted,
   `NETBOX_MCP_ENABLE_WRITES=true` and `MCP_PUBLISH_PORT`.
2. **Create a token per person** — each run prints the raw token (give it to that person) and a
   hashed entry for the server, so `.env` never holds a usable secret:

   ```bash
   uv run netbox-mcp token alice
   # or, without a local Python setup:
   docker compose run --rm --no-deps netbox-mcp netbox-mcp token alice
   ```

   Add the printed `alice:sha256:...` entries to `MCP_AUTH_TOKENS` (comma-separated). To revoke
   someone, delete their entry and `docker compose up -d`.
3. **Start**:

   ```bash
   docker compose up -d --build
   docker compose ps        # should show (healthy)
   docker compose logs -f   # each tool call is logged with the caller's token name
   ```

4. **Connect a client** to `http://<host>:<MCP_PUBLISH_PORT>/mcp` with the header
   `Authorization: Bearer <token>`. For Claude Code:

   ```bash
   claude mcp add --transport http netbox-remote http://<host>:8000/mcp \
     --header "Authorization: Bearer <token>"
   ```

**Claude Desktop / claude.ai custom connectors** connect from Anthropic's cloud, not from your
machine, so the URL must be reachable from the internet over HTTPS. Put the container behind a
reverse proxy or tunnel that terminates TLS (Caddy, Traefik, nginx, Cloudflare Tunnel, ...) and use
`https://<public-name>/mcp`. Keep plain HTTP for trusted LAN/VPN use only.

## Tools

| Tool | Purpose |
|---|---|
| `netbox_status` | Connection check: NetBox version, API user, writes enabled? |
| `netbox_search` | Search many object types at once (devices, VMs, IPs, prefixes, VLANs, cables, ...) |
| `netbox_lookup_ip` | IP → address object, assigned device/VM + interface, containing prefixes, VLAN, VRF |
| `netbox_lookup_mac` | MAC (any format) → interface, device/VM and its IPs |
| `netbox_device_summary` | Device or VM overview with every interface's IPs, VLANs, LAG, MAC, cable and peer |
| `netbox_interface_detail` | One interface in depth (abbreviations like `Gi1/0/1` work) |
| `netbox_trace_cable` | End-to-end cable path through patch panels and circuits |
| `netbox_prefix_utilization` | Utilisation (NetBox's rules), child prefixes, first free IP / free blocks |
| `netbox_next_available_ips` | Peek at the next free IPs in a prefix |
| `netbox_next_available_prefixes` | Peek at free blocks, or the next N child prefixes of a given length |
| `netbox_vlan_lookup` | VLAN by VID/name with its prefixes and tagged/untagged interfaces |
| `netbox_list_object_types` | Object types usable with the generic tools, grouped by app |
| `netbox_list_objects` | List/filter any object type (filters are validated against the schema) |
| `netbox_get_object` | One object by type + ID |
| `netbox_search_spec` | Keyword search over NetBox's OpenAPI spec |
| `netbox_get_definition` | Fields, enum choices, writable/required fields and filters for a type |
| `netbox_api_call` | Any REST endpoint (GET only unless writes are enabled) |

### Write tools (only when `NETBOX_MCP_ENABLE_WRITES=true`)

| Tool | Purpose |
|---|---|
| `netbox_create_object` | Create one or many objects of any type |
| `netbox_update_object` | Partial update of one object, or the same change across many |
| `netbox_delete_object` | Delete by id (all ids must exist, or nothing is deleted) |
| `netbox_allocate_ips` | Atomically reserve the next free IP(s); optionally assign to an interface and set as primary |
| `netbox_allocate_prefix` | Atomically carve the next free child prefix(es) of a given length |

With writes enabled, `netbox_api_call` also accepts POST/PUT/PATCH/DELETE. When writes are off, these
tools are not registered at all. NetBox silently ignores unknown field names on create/update, so the
server checks every field name against the schema first and suggests corrections for typos. Writes
are logged to the `netbox_mcp.audit` logger.

Results are compact JSON: URLs, empty values and zero counts are dropped and nested objects are
reduced to `{id, display}`. Pass `response_format="markdown"` for a table.

The NetBox OpenAPI schema is fetched on first use and cached as a compact index in
`NETBOX_MCP_CACHE_DIR` (default `~/.cache/netbox-mcp`), keyed by NetBox URL and version.

## Development

```bash
uv run pytest          # unit tests (mocked NetBox)
uv run ruff check .
uv run mypy
```

Integration tests run read-only checks against a live NetBox seeded with a small lab topology
(everything tagged `mcp-test`):

```bash
uv run python scripts/seed_lab.py            # create the lab (safe to re-run)
uv run pytest tests/integration
uv run python scripts/seed_lab.py --delete   # remove it again
```

Write-path integration tests create and delete their own objects, so they are opt-in:

```bash
NETBOX_MCP_TEST_WRITES=1 uv run pytest tests/integration
```
