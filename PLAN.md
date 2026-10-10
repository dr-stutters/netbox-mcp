# NetBox MCP Server — Plan

A FastMCP server that lets Claude query (and, when explicitly enabled, modify) a NetBox instance.
Built from scratch; no code reused from other MCP servers.

## 1. Scope decisions

| Area | Decision |
|---|---|
| Target | NetBox Community **4.7.2** (netbox-docker 5.1.1), vanilla — no plugins |
| Framework | FastMCP (Python), latest stable at build time, pinned |
| NetBox client | `httpx.AsyncClient`, direct REST (no pynetbox) |
| Access mode | Read-only by default; write tools registered only when `NETBOX_MCP_ENABLE_WRITES=true` |
| Write guardrails | Env flag only (no dry-run/confirm layer) |
| NetBox credentials | One shared service token |
| Transports | **stdio** (Claude Code, local) and **Streamable HTTP** (Docker, Claude Desktop custom connector) |
| MCP auth (HTTP) | `Authorization: Bearer <token>` checked against configured tokens; none for stdio |
| NetBox connection | HTTPS (with verify / custom CA / insecure options) **or** plain HTTP |
| Output | Compact JSON by default; `response_format="markdown"` optional |
| Tool design | Hybrid: curated task tools + generic CRUD + OpenAPI escape hatches |
| Deployment | Own compose file on a separate Docker host, reaching NetBox over the network |
| Users | Multiple people |
| Out of scope (v1) | CML integration (later phase), OAuth/IdP, per-user NetBox tokens, plugins |

## 2. Architecture

```
Claude Code ──stdio──┐
                     ├─► FastMCP server ──httpx (Token/Bearer)──► NetBox 4.7 REST API
Claude Desktop ─HTTP─┘   (bearer auth on HTTP only)                 /api/..., /api/schema/
```

### Project layout

```
netbox-mcp/
├── pyproject.toml            # uv-managed, Python 3.12+
├── src/netbox_mcp/
│   ├── __main__.py           # CLI: --transport stdio|http, --host, --port
│   ├── server.py             # FastMCP app, tool registration, write gating
│   ├── config.py             # pydantic-settings, env vars (below)
│   ├── auth.py               # bearer token verifier for HTTP transport
│   ├── client.py             # NetBoxClient: httpx, pagination, errors, retries
│   ├── schema.py             # OpenAPI schema loader + search/index (cached)
│   ├── formatting.py         # compact JSON trimming + markdown renderer
│   ├── objects.py            # object-type registry ("dcim.devices" → endpoint)
│   └── tools/
│       ├── search.py         # global search, IP/MAC lookup
│       ├── devices.py        # device & interface summary
│       ├── ipam.py           # prefix utilisation, next-available, VLAN/VRF
│       ├── cabling.py        # cable path trace
│       ├── generic.py        # list/get/create/update/delete for any type
│       └── spec.py           # search_spec, get_definition, api_call
├── tests/
│   ├── unit/                 # respx-mocked httpx
│   └── integration/          # opt-in, against a live/throwaway NetBox
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

## 3. Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `NETBOX_URL` | — (required) | e.g. `https://netbox.lab.local` or `http://10.0.0.5:8000` |
| `NETBOX_TOKEN` | — (required) | Shared service token |
| `NETBOX_VERIFY_SSL` | `true` | Set `false` for self-signed lab certs |
| `NETBOX_CA_BUNDLE` | — | Path to an internal CA PEM (takes precedence over verify flag) |
| `NETBOX_TIMEOUT` | `30` | Request timeout (s) |
| `NETBOX_MCP_ENABLE_WRITES` | `false` | Registers create/update/delete tools |
| `NETBOX_MCP_CACHE_DIR` | `~/.cache/netbox-mcp` | Disk cache for the compact schema index (empty = off) |
| `MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `MCP_HOST` / `MCP_PORT` | `0.0.0.0` / `8000` | HTTP bind |
| `MCP_AUTH_TOKENS` | — | Comma-separated `name:token` pairs; required when transport is `http` |
| `LOG_LEVEL` | `INFO` | |

Notes:
- **Token header**: NetBox 4.5+ supports v2 tokens (`nbt_…`, sent as `Authorization: Bearer`) alongside legacy v1 (`Authorization: Token`). The client auto-detects by prefix, so either works.
- **HTTP vs HTTPS**: driven purely by the scheme in `NETBOX_URL`. Log a warning at startup when using `http://` (token travels in clear text).
- **Per-person MCP tokens**: `name:token` pairs let each user have their own bearer token, so logs show *who* called which tool, and one person's access can be revoked without affecting others — even though NetBox sees a single service user.
- Server refuses to start in HTTP mode without `MCP_AUTH_TOKENS` (fail closed).

## 4. Tools (v1)

All tools have explicit Pydantic-typed params, clear docstrings, and MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`). Reads accept `limit`/`offset` (default limit 50, max 1000) and `response_format`.

### Curated — read
| Tool | What it does | NetBox endpoints |
|---|---|---|
| `netbox_search` | Free-text search across devices, IPs, prefixes, VLANs, sites, circuits, VMs | `?q=` across key endpoints, merged & ranked |
| `netbox_lookup_ip` | IP → address object, parent prefix(es), VRF, assigned interface/device/VM, site, VLAN | `ipam/ip-addresses`, `ipam/prefixes?contains=` |
| `netbox_lookup_mac` | MAC → interface(s), device/VM, IPs on that interface | `dcim/mac-addresses`, `dcim/interfaces?mac_address=` |
| `netbox_device_summary` | Device detail + role/platform/site/rack, primary IPs, interfaces (status, IPs, VLANs, cable, connected peer) | `dcim/devices`, `dcim/interfaces?device_id=`, `ipam/ip-addresses?device_id=` |
| `netbox_interface_detail` | Single interface: IPs, VLAN mode/tagging, cable, link peer, LAG membership | `dcim/interfaces/{id}` |
| `netbox_trace_cable` | Full cable path from an interface/port to its far end | `dcim/interfaces/{id}/trace/` (and front/rear ports, console ports) |
| `netbox_prefix_utilization` | Prefix utilisation %, child prefixes, used/free counts | `ipam/prefixes/{id}`, `?parent=` |
| `netbox_next_available_ips` | Next N free IPs in a prefix (read-only peek) | `ipam/prefixes/{id}/available-ips/` (GET) |
| `netbox_next_available_prefixes` | Free child prefixes of a given length | `ipam/prefixes/{id}/available-prefixes/` (GET) |
| `netbox_vlan_lookup` | VLAN by VID/name/group/site, with prefixes and interfaces using it | `ipam/vlans`, `ipam/prefixes?vlan_id=` |

### Curated — write (only when writes enabled)
| Tool | What it does |
|---|---|
| `netbox_allocate_ips` | POST to `available-ips` to atomically reserve N IPs (optional description/assignment) |
| `netbox_allocate_prefix` | POST to `available-prefixes` to carve a child prefix |

### Generic
| Tool | Writes? | Notes |
|---|---|---|
| `netbox_list_object_types` | no | Lists supported `app.model` keys (from schema) |
| `netbox_list_objects` | no | `object_type`, `filters` dict, `fields`, `brief`, `ordering`, pagination |
| `netbox_get_object` | no | By ID |
| `netbox_create_object` | yes | Accepts single or bulk list |
| `netbox_update_object` | yes | PATCH semantics |
| `netbox_delete_object` | yes | Bulk by ID list supported |

### Escape hatches
| Tool | Writes? | Notes |
|---|---|---|
| `netbox_search_spec` | no | Keyword search over the OpenAPI schema (paths, operations, models) |
| `netbox_get_definition` | no | Full schema for a model or operation (fields, enums, required) |
| `netbox_api_call` | GET only unless writes enabled | Arbitrary `/api/...` call; method restricted to GET when read-only |

OpenAPI schema is fetched once from `/api/schema/?format=json`, cached in memory (with an optional on-disk cache) and refreshed on demand.

## 5. Cross-cutting behaviour

- **Token efficiency**: use NetBox `brief=true` for nested lookups and `fields=` to trim; strip `url`, `display_url`, empty `custom_fields`/`tags`, etc. Truncate large results with a clear "N more — use offset" hint.
- **Pagination**: transparent `next`-link following up to the requested limit; never fetch unbounded.
- **Errors**: map NetBox 400 validation errors into concise, model-actionable messages (field → problem); 401/403 → token/permission hints; 404 → "not found, try netbox_search"; network/TLS errors → a hint about `NETBOX_VERIFY_SSL`/`NETBOX_CA_BUNDLE`.
- **Retries**: limited retry with backoff on 429/502/503/504 and connection errors (GETs only).
- **Write gating**: write tools are *not registered* when disabled (invisible to the model), and `netbox_api_call` rejects non-GET methods.
- **Logging**: structured logs to stderr (never stdout — that would corrupt stdio), including caller name (from bearer token), tool, object type and, for writes, the IDs changed.
- **Health**: `/health` endpoint on HTTP transport (unauthenticated, no NetBox data) for Docker healthcheck; startup check of `/api/status/` that logs the NetBox version.

## 6. Tooling

- **uv** + `pyproject.toml`, Python 3.12+
- **pytest** + **pytest-asyncio** + **respx** for unit tests (mocked NetBox)
- **Integration tests** (marked, opt-in) against a live NetBox via `NETBOX_URL`/`NETBOX_TOKEN`; read-only by default, write tests only when a flag is set and only touching objects tagged `mcp-test`
- **ruff** (lint + format), **mypy** (strict on `src/`)
- **pre-commit** hooks; GitHub Actions CI (lint, type check, unit tests, Docker build) — added once a remote exists

## 7. Deployment

- **Local (Claude Code)**: `uv run netbox-mcp` with stdio; registered via `claude mcp add netbox -- uv --directory /home/reptar/Netbox-MCP run netbox-mcp` and env vars.
- **Docker (Claude Desktop)**:
  - Multi-stage `Dockerfile` (uv builder → slim runtime), non-root user, `HEALTHCHECK` on `/health`.
  - `docker-compose.yml` on the separate Docker host, `env_file: .env`, optional CA bundle mounted read-only, `MCP_TRANSPORT=http`.
  - Claude Desktop connects to `http(s)://<docker-host>:8000/mcp` with an `Authorization: Bearer <token>` header (same pattern as your existing servers).
  - TLS for the MCP endpoint itself: document putting it behind a reverse proxy if it leaves a trusted network; not built into the app.

## 8. Milestones

1. ✅ **Skeleton** — uv project, config, NetBoxClient (auth header detection, HTTP/HTTPS/CA, pagination, errors), stdio server with a `netbox_status` tool. Verify against your 4.7.2 instance.
2. ✅ **Generic + schema tools** — object-type registry, list/get, `search_spec`, `get_definition`, read-only `api_call`. Unit tests.
3. ✅ **Curated reads** — search, IP/MAC lookup, device/interface summary, cable trace, IPAM helpers, VLAN lookup. Unit + integration tests.
4. ✅ **Writes** — env-gated generic create/update/delete, allocate IPs/prefix, `api_call` write methods.
5. ✅ **HTTP + auth + Docker** — Streamable HTTP, bearer verifier with named tokens, `/health`, Dockerfile, compose, README with Claude Code & Desktop setup.
6. **Hardening** — eval questions (10 realistic NetBox Q&As run through Claude), output-size tuning, CI.
7. **Later: CML** — NetBox as source of truth for CML labs (design TBD).

## 9. Open items to confirm during build

- FastMCP version at build time and its current bearer-token verifier API (custom `TokenVerifier` vs built-in static verifier).
- Whether your NetBox service token is v1 or v2 (`nbt_` prefix) — handled either way.
- A NetBox user/token permission set for the service account: read-all now; write permissions only when you enable writes.
