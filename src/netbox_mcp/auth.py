"""Bearer-token auth for the HTTP transport.

Tokens are configured as named entries so logs show who called what and each person can be revoked
individually. Entries can hold the raw token or, preferably, its SHA-256 hash so the server's
config never contains a usable secret:

    MCP_AUTH_TOKENS="mitchell:sha256:9f86d0...,alex:Kq3...raw-token..."

Generate entries with `netbox-mcp token <name>`.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken

MIN_TOKEN_LENGTH = 24
_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def parse_tokens(spec: str) -> dict[str, str]:
    """Parse 'name:token' / 'name:sha256:<hex>' entries (comma- or newline-separated, '#' comments)
    into {name: sha256_hex}."""
    entries: dict[str, str] = {}
    for position, raw in enumerate(re.split(r"[,\n]", spec), start=1):
        entry = raw.split("#", 1)[0].strip()
        if not entry:
            continue
        name, sep, value = entry.partition(":")
        name, value = name.strip(), value.strip()
        if not sep or not name or not value:
            # Never echo the entry: it may be a bare token.
            raise ValueError(f"Token entry #{position} must look like 'name:token' or 'name:sha256:<hex>'")
        if not _NAME_RE.match(name):
            raise ValueError(f"Token name {name!r} may only contain letters, digits and _ . @ -")
        if name in entries:
            raise ValueError(f"Duplicate token name {name!r}")
        if value.startswith("sha256:"):
            digest = value[len("sha256:") :].lower()
            if not _HASH_RE.match(digest):
                raise ValueError(f"Token {name!r}: sha256 value must be 64 hex characters")
        else:
            if len(value) < MIN_TOKEN_LENGTH:
                raise ValueError(f"Token {name!r} is too short (min {MIN_TOKEN_LENGTH} characters)")
            digest = hash_token(value)
        entries[name] = digest
    if len(set(entries.values())) != len(entries):
        raise ValueError("Two token names share the same token")
    return entries


class NamedTokenVerifier(TokenVerifier):
    """Accepts a bearer token if its SHA-256 matches a configured entry; client_id is the entry name."""

    def __init__(self, tokens: dict[str, str]) -> None:
        super().__init__()
        if not tokens:
            raise ValueError("At least one token is required")
        self._tokens = dict(tokens)

    @property
    def names(self) -> list[str]:
        return sorted(self._tokens)

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hash_token(token)
        match = None
        # Compare against every entry so timing doesn't reveal which (if any) matched.
        for name, expected in self._tokens.items():
            if hmac.compare_digest(digest, expected):
                match = name
        if match is None:
            return None
        return AccessToken(token=token, client_id=match, scopes=[])
