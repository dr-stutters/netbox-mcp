"""Runtime configuration, loaded from environment variables and an optional .env file."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # NetBox connection
    netbox_url: str
    netbox_token: SecretStr
    netbox_verify_ssl: bool = True
    netbox_ca_bundle: Path | None = None
    netbox_timeout: float = Field(default=30.0, gt=0)

    # Behaviour
    netbox_mcp_enable_writes: bool = False
    # Where the compact OpenAPI schema index is cached; set empty to disable the disk cache.
    netbox_mcp_cache_dir: Path | None = Path.home() / ".cache" / "netbox-mcp"

    # MCP transport
    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8000

    log_level: str = "INFO"

    @field_validator("netbox_url")
    @classmethod
    def _normalise_url(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if v.endswith("/api"):
            v = v[: -len("/api")]
        if not v.startswith(("http://", "https://")):
            raise ValueError("NETBOX_URL must start with http:// or https://")
        return v

    @field_validator("netbox_mcp_cache_dir", mode="before")
    @classmethod
    def _cache_dir(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v.strip()).expanduser() if v.strip() else None  # empty disables the cache
        return v

    @field_validator("netbox_token")
    @classmethod
    def _strip_auth_scheme(cls, v: SecretStr) -> SecretStr:
        # Tolerate tokens pasted with their header scheme, e.g. "Bearer nbt_..." or "Token abc...".
        raw = v.get_secret_value().strip()
        for prefix in ("Bearer ", "Token "):
            if raw.startswith(prefix):
                raw = raw[len(prefix) :].strip()
        if not raw:
            raise ValueError("NETBOX_TOKEN is empty")
        return SecretStr(raw)

    @property
    def verify(self) -> bool | str:
        """Value for httpx's `verify` argument."""
        if self.netbox_ca_bundle is not None:
            return str(self.netbox_ca_bundle)
        return self.netbox_verify_ssl
