"""FastMCP server for NetBox."""

from __future__ import annotations

import argparse
import logging
import sys

from pydantic import ValidationError


def _token_command(name: str) -> None:
    from netbox_mcp.auth import _NAME_RE, generate_token, hash_token

    if not _NAME_RE.match(name):
        sys.exit("netbox-mcp: token name may only contain letters, digits and _ . @ -")
    token = generate_token()
    print(f"Bearer token for {name!r} (give this to the user; it is not stored anywhere):\n")
    print(f"  {token}\n")
    print("Add this entry to MCP_AUTH_TOKENS on the server (comma-separate multiple entries):\n")
    print(f"  {name}:sha256:{hash_token(token)}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="netbox-mcp", description="MCP server for NetBox")
    parser.add_argument("--transport", choices=["stdio", "http"], help="overrides MCP_TRANSPORT")
    sub = parser.add_subparsers(dest="command")
    tok = sub.add_parser("token", help="generate a bearer token for the HTTP transport")
    tok.add_argument("name", help="who the token is for, e.g. alice")
    args = parser.parse_args()

    if args.command == "token":
        _token_command(args.name)
        return

    from netbox_mcp.auth import NamedTokenVerifier
    from netbox_mcp.config import Settings
    from netbox_mcp.server import create_server

    try:
        settings = Settings()  # type: ignore[call-arg]  # populated from env
    except ValidationError as e:
        sys.exit(f"netbox-mcp: invalid configuration:\n{e}")

    # Logs must go to stderr: stdout carries the stdio protocol.
    logging.basicConfig(
        level=settings.log_level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("netbox_mcp")

    transport = args.transport or settings.mcp_transport
    if transport == "stdio":
        create_server(settings).run(transport="stdio", show_banner=False)
        return

    # HTTP: fail closed. Never serve NetBox over the network without authentication.
    try:
        tokens = settings.auth_token_hashes()
    except (ValueError, OSError) as e:
        sys.exit(f"netbox-mcp: invalid MCP_AUTH_TOKENS / MCP_AUTH_TOKENS_FILE: {e}")
    if not tokens:
        sys.exit(
            "netbox-mcp: HTTP transport requires MCP_AUTH_TOKENS (or MCP_AUTH_TOKENS_FILE). "
            "Generate one with: netbox-mcp token <name>"
        )
    auth = NamedTokenVerifier(tokens)
    log.info(
        "HTTP transport on %s:%s%s; %d token(s): %s; writes %s",
        settings.mcp_host,
        settings.mcp_port,
        settings.mcp_path,
        len(tokens),
        ", ".join(auth.names),
        "ENABLED" if settings.netbox_mcp_enable_writes else "disabled",
    )
    create_server(settings, auth=auth).run(
        transport="http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        path=settings.mcp_path,
        show_banner=False,
    )
