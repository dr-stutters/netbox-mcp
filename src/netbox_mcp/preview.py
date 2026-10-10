"""Preview-then-confirm for write tools.

Every write tool first returns a preview of exactly what it would do, plus a single-use confirm token.
Applying requires calling the same tool again with identical arguments and that token. The preview is
recomputed at confirm time, and the token is refused if it expired, was already used, belongs to
another caller, the arguments differ, or the target objects changed since the preview.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from netbox_mcp.errors import InvalidRequest

TOKEN_TTL_SECONDS = 600


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class Plan:
    """What a write would do: a one-line summary, details for the user, anything that would make it
    fail, and a fingerprint of the current state it depends on."""

    summary: str
    details: dict[str, Any]
    fingerprint: Any = None
    blockers: list[str] = field(default_factory=list)


@dataclass
class _Pending:
    tool: str
    args: str
    fingerprint: str
    caller: str
    expires: float


class PreviewStore:
    def __init__(self, ttl: float = TOKEN_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._pending: dict[str, _Pending] = {}

    def _purge(self) -> None:
        now = time.monotonic()
        for token in [t for t, p in self._pending.items() if p.expires < now]:
            del self._pending[token]

    def issue(self, tool: str, args: dict[str, Any], fingerprint: Any, caller: str) -> str:
        self._purge()
        token = "pv_" + secrets.token_urlsafe(12)
        self._pending[token] = _Pending(
            tool, _digest(args), _digest(fingerprint), caller, time.monotonic() + self.ttl
        )
        return token

    def redeem(self, token: str, tool: str, args: dict[str, Any], fingerprint: Any, caller: str) -> None:
        """Consume a token, or raise explaining why it can't be used. Any failed attempt other than a
        typo'd token also invalidates it, so a fresh preview is always required after a mismatch."""
        self._purge()
        pending = self._pending.get(token)
        if pending is None:
            raise InvalidRequest(
                "Unknown or expired confirm token (tokens last 10 minutes and work once). "
                "Call the tool again without confirm to get a fresh preview."
            )
        problem = None
        if pending.caller != caller:
            problem = "it was issued to a different user"
        elif pending.tool != tool:
            problem = f"it was issued for {pending.tool}"
        elif pending.args != _digest(args):
            problem = "the arguments differ from the previewed call"
        elif pending.fingerprint != _digest(fingerprint):
            problem = "the objects involved changed since the preview"
        del self._pending[token]
        if problem:
            raise InvalidRequest(f"Confirm token refused: {problem}. Nothing was changed; preview again.")


async def preview_or_apply(
    store: PreviewStore,
    *,
    tool: str,
    args: dict[str, Any],
    confirm: str | None,
    caller: str,
    plan: Plan,
    apply: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Return a preview (issuing a token) when confirm is None; otherwise check the token against the
    freshly computed plan and apply."""
    if confirm is None:
        out: dict[str, Any] = {"status": "preview", "action": plan.summary, "preview": plan.details}
        if plan.blockers:
            out["status"] = "blocked"
            out["blockers"] = plan.blockers
            out["note"] = "This change can't be applied as it stands; nothing was changed."
            return out
        token = store.issue(tool, args, plan.fingerprint, caller)
        out["confirm"] = token
        out["expires_in_seconds"] = int(store.ttl)
        out["how_to_apply"] = (
            "Nothing has been changed. Show this preview to the user; if they approve, call "
            f'{tool} again with exactly the same arguments plus confirm="{token}".'
        )
        return out
    if plan.blockers:
        raise InvalidRequest(f"Can't apply: {'; '.join(plan.blockers)}. Nothing was changed.")
    store.redeem(confirm, tool, args, plan.fingerprint, caller)
    result = await apply()
    if "status" in result:  # e.g. a script job's own status; "status" here means applied
        result["result_status"] = result.pop("status")
    return {"status": "applied", "action": plan.summary, **result}
