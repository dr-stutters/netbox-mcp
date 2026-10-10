"""Async NetBox REST client: auth, pagination, retries and error mapping."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from netbox_mcp.errors import NetBoxError

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 502, 503, 504}
MAX_RETRIES = 2
PAGE_SIZE = 250


def auth_header(token: str) -> str:
    """v2 tokens (NetBox 4.5+) start with 'nbt_' and use Bearer; legacy v1 tokens use Token."""
    return f"Bearer {token}" if token.startswith("nbt_") else f"Token {token}"


def _format_validation_errors(body: Any) -> str:
    if isinstance(body, dict):
        if "detail" in body and len(body) == 1:
            return str(body["detail"])
        parts = []
        for field, problems in body.items():
            if isinstance(problems, list):
                problems = "; ".join(str(p) for p in problems)
            parts.append(f"{field}: {problems}")
        return " | ".join(parts)
    if isinstance(body, list):
        return " || ".join(_format_validation_errors(item) for item in body if item)
    return str(body)


def _error_from_response(resp: httpx.Response) -> NetBoxError:
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text[:500]
    code = resp.status_code
    detail = _format_validation_errors(body)
    if code == 400:
        msg = f"NetBox rejected the request (400): {detail}"
    elif code in (401, 403):
        msg = (
            f"NetBox denied access ({code}): {detail}. Check NETBOX_TOKEN is valid, not expired, "
            "and that its user has permission for this object type/action."
        )
    elif code == 404:
        msg = f"Not found (404): {resp.request.url.path}. Check the ID/path, or search for the object first."
    else:
        msg = f"NetBox returned HTTP {code}: {detail}"
    return NetBoxError(msg, code)


class NetBoxClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        verify: bool | str = True,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/",
            headers={
                "Authorization": auth_header(token),
                "Accept": "application/json",
                "User-Agent": "netbox-mcp",
            },
            verify=verify,
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> NetBoxClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout: float | None = None,  # noqa: ASYNC109 - per-request HTTP timeout, not a cancel scope
    ) -> Any:
        """Send a request to /api/<path>. Returns parsed JSON, or None for empty responses."""
        path = path.lstrip("/")
        if path.startswith("api/"):
            path = path[len("api/") :]
        method = method.upper()
        retries = MAX_RETRIES if method == "GET" else 0

        for attempt in range(retries + 1):
            try:
                resp = await self._http.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                )
            except httpx.ConnectError as e:
                if attempt < retries:
                    await asyncio.sleep(0.5 * 2**attempt)
                    continue
                hint = ""
                if "SSL" in str(e) or "certificate" in str(e).lower():
                    hint = (
                        " TLS verification failed: set NETBOX_CA_BUNDLE, or NETBOX_VERIFY_SSL=false for labs."
                    )
                raise NetBoxError(f"Could not connect to NetBox at {self.base_url}: {e}.{hint}") from e
            except httpx.TimeoutException as e:
                if attempt < retries:
                    await asyncio.sleep(0.5 * 2**attempt)
                    continue
                raise NetBoxError(f"NetBox request timed out: {method} /api/{path}") from e

            if resp.status_code in RETRY_STATUSES and attempt < retries:
                log.warning("NetBox %s on %s /api/%s; retrying", resp.status_code, method, path)
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if resp.is_error:
                raise _error_from_response(resp)
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

        raise AssertionError("unreachable")

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def get_object(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a single object."""
        obj: dict[str, Any] = await self.get(path, params)
        return obj

    async def list(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch up to `limit` results starting at `offset`. Returns (results, total_count)."""
        results: list[dict[str, Any]] = []
        count = 0
        query = dict(params or {})
        while len(results) < limit:
            query["limit"] = min(PAGE_SIZE, limit - len(results))
            query["offset"] = offset + len(results)
            page = await self.get(path, query)
            count = page.get("count", 0)
            batch = page.get("results", [])
            results.extend(batch)
            if not batch or not page.get("next"):
                break
        return results, count

    async def status(self) -> dict[str, Any]:
        result: dict[str, Any] = await self.get("status/")
        return result

    async def get_text(self, path: str, params: dict[str, Any] | None = None) -> tuple[str, str]:
        """GET a non-JSON response (e.g. a rendered export template). Returns (text, content type)."""
        try:
            resp = await self._http.get(path.lstrip("/"), params=params)
        except httpx.HTTPError as e:
            raise NetBoxError(f"Could not reach NetBox at {self.base_url}: {e}") from e
        if resp.is_error:
            raise _error_from_response(resp)
        return resp.text, resp.headers.get("content-type", "")

    async def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a GraphQL query against /graphql/ (outside the REST API's /api/ prefix)."""
        try:
            resp = await self._http.post(
                f"{self.base_url}/graphql/", json={"query": query, "variables": variables or {}}
            )
        except httpx.HTTPError as e:
            raise NetBoxError(f"Could not reach NetBox at {self.base_url}: {e}") from e
        if resp.is_error:
            raise _error_from_response(resp)
        result: dict[str, Any] = resp.json()
        return result
