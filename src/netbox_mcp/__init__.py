"""FastMCP server for NetBox."""

from __future__ import annotations

import argparse
import logging
import sys

from pydantic import ValidationError


def main() -> None:
    from netbox_mcp.config import Settings
    from netbox_mcp.server import create_server

    parser = argparse.ArgumentParser(prog="netbox-mcp", description="MCP server for NetBox")
    parser.add_argument("--transport", choices=["stdio", "http"], help="overrides MCP_TRANSPORT")
    args = parser.parse_args()

    try:
        settings = Settings()  # type: ignore[call-arg]  # populated from env
    except ValidationError as e:
        print(f"netbox-mcp: invalid configuration:\n{e}", file=sys.stderr)
        sys.exit(2)

    # Logs must go to stderr: stdout carries the stdio protocol.
    logging.basicConfig(
        level=settings.log_level.upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    transport = args.transport or settings.mcp_transport
    if transport == "http":
        # Fail closed until bearer-token auth is implemented for the HTTP transport.
        print("netbox-mcp: HTTP transport is not available yet (requires auth)", file=sys.stderr)
        sys.exit(2)

    mcp = create_server(settings)
    mcp.run(transport="stdio", show_banner=False)
