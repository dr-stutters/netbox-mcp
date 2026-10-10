import sys
from pathlib import Path

import pytest
import respx
from starlette.testclient import TestClient

import netbox_mcp
from netbox_mcp.auth import NamedTokenVerifier, hash_token, parse_tokens
from netbox_mcp.config import Settings
from netbox_mcp.server import create_server

TOKEN_A = "a" * 32
TOKEN_B = "b-token-" + "x" * 30


def test_parse_tokens_raw_hashed_and_comments() -> None:
    spec = f"""
        alice:{TOKEN_A}          # raw token
        bob:sha256:{hash_token(TOKEN_B).upper()}
    """
    assert parse_tokens(spec) == {"alice": hash_token(TOKEN_A), "bob": hash_token(TOKEN_B)}
    assert parse_tokens(f"alice:{TOKEN_A},bob:{TOKEN_B}") == parse_tokens(spec)
    assert parse_tokens("") == {}


@pytest.mark.parametrize(
    ("spec", "error"),
    [
        ("alice:short", "too short"),
        (f"al ice:{TOKEN_A}", "may only contain"),
        (f"alice:{TOKEN_A},alice:{TOKEN_B}", "Duplicate"),
        (f"alice:{TOKEN_A},bob:{TOKEN_A}", "same token"),
        ("alice:sha256:abc", "64 hex"),
    ],
)
def test_parse_tokens_errors(spec: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        parse_tokens(spec)


def test_parse_tokens_never_echoes_a_bare_token() -> None:
    secret = "s3cr3t-value-that-must-not-leak-anywhere"
    with pytest.raises(ValueError) as exc:
        parse_tokens(secret)
    assert secret not in str(exc.value)
    assert "entry #1" in str(exc.value)


async def test_verifier() -> None:
    v = NamedTokenVerifier(parse_tokens(f"alice:{TOKEN_A},bob:sha256:{hash_token(TOKEN_B)}"))
    ok = await v.verify_token(TOKEN_B)
    assert ok is not None and ok.client_id == "bob"
    assert await v.verify_token("wrong-token") is None
    assert v.names == ["alice", "bob"]


def test_settings_merge_env_and_file(tmp_path: Path) -> None:
    f = tmp_path / "tokens"
    f.write_text(f"bob:{TOKEN_B}\n")
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        netbox_url="http://netbox.test",
        netbox_token="nbt_x.y",
        mcp_auth_tokens=f"alice:{TOKEN_A}",
        mcp_auth_tokens_file=f,
    )
    assert set(s.auth_token_hashes()) == {"alice", "bob"}


def test_token_command_output_round_trips(capsys: pytest.CaptureFixture[str]) -> None:
    netbox_mcp._token_command("carol")
    lines = [ln.strip() for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    token = lines[1]
    entry = lines[-1]
    assert parse_tokens(entry) == {"carol": hash_token(token)}


def test_http_requires_tokens(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    monkeypatch.setenv("NETBOX_URL", "http://netbox.test")
    monkeypatch.setenv("NETBOX_TOKEN", "nbt_x.y")
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.delenv("MCP_AUTH_TOKENS", raising=False)
    monkeypatch.setattr(sys, "argv", ["netbox-mcp"])
    with pytest.raises(SystemExit, match="requires MCP_AUTH_TOKENS"):
        netbox_mcp.main()


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def test_http_app_auth(settings: Settings, netbox: respx.MockRouter) -> None:
    mcp = create_server(settings, auth=NamedTokenVerifier(parse_tokens(f"alice:{TOKEN_A}")))
    with TestClient(mcp.http_app(path="/mcp")) as client:
        health = client.get("/health")
        assert health.status_code == 200 and health.json() == {"status": "ok"}

        assert client.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
        bad = {**HEADERS, "Authorization": "Bearer nope"}
        assert client.post("/mcp", json=INIT, headers=bad).status_code == 401

        good = {**HEADERS, "Authorization": f"Bearer {TOKEN_A}"}
        assert client.post("/mcp", json=INIT, headers=good).status_code == 200
