import pytest
from pydantic import ValidationError

from netbox_mcp.config import Settings


@pytest.fixture(scope="module")
def settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError:
        pytest.skip("NETBOX_URL / NETBOX_TOKEN not configured")
