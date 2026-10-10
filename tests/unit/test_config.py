from pathlib import Path

import pytest
from pydantic import ValidationError

from netbox_mcp.config import Settings


def make(**kw: object) -> Settings:
    base: dict[str, object] = {"netbox_url": "http://nb:8030", "netbox_token": "nbt_x.y"}
    base.update(kw)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def test_url_normalised() -> None:
    assert make(netbox_url="http://nb:8030/api/").netbox_url == "http://nb:8030"


def test_url_requires_scheme() -> None:
    with pytest.raises(ValidationError):
        make(netbox_url="nb:8030")


@pytest.mark.parametrize("raw", ["Bearer nbt_x.y", "  nbt_x.y  ", "Token nbt_x.y"])
def test_token_scheme_prefix_stripped(raw: str) -> None:
    assert make(netbox_token=raw).netbox_token.get_secret_value() == "nbt_x.y"


def test_verify_prefers_ca_bundle() -> None:
    assert make(netbox_verify_ssl=False).verify is False
    assert make(netbox_ca_bundle="/ca.pem").verify == "/ca.pem"


def test_writes_disabled_by_default() -> None:
    assert make().netbox_mcp_enable_writes is False


def test_cache_dir_expands_home_and_empty_disables() -> None:
    assert make(netbox_mcp_cache_dir="~/x").netbox_mcp_cache_dir == Path.home() / "x"
    assert make(netbox_mcp_cache_dir="").netbox_mcp_cache_dir is None
