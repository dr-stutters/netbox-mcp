"""Automation hooks: custom scripts, background jobs, export templates and GraphQL."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from netbox_mcp.client import NetBoxClient
from netbox_mcp.config import Settings
from netbox_mcp.deps import current_caller, get_netbox, get_previews, get_schema
from netbox_mcp.errors import InvalidRequest
from netbox_mcp.formatting import MAX_RESPONSE_CHARS, compact, compact_dict
from netbox_mcp.preview import Plan, preview_or_apply
from netbox_mcp.tools.common import choice, content_type, label
from netbox_mcp.tools.config import check_custom_field_filters

audit = logging.getLogger("netbox_mcp.audit")

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
RUN = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)

FINISHED = {"completed", "errored", "failed"}
MAX_LOG_LINES = 100
_MUTATION_RE = re.compile(r"^\s*(mutation|subscription)\b", re.IGNORECASE)


def _strip_graphql_comments(query: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in query.splitlines())


def _script_row(s: dict[str, Any]) -> dict[str, Any]:
    last = s.get("result") or {}
    return compact_dict(
        {
            "id": s["id"],
            "name": s.get("name"),
            "module": (s.get("display") or "").partition("(")[2].rstrip(")") or None,
            "description": s.get("description"),
            "inputs": s.get("vars"),
            "executable": s.get("is_executable"),
            "last_run": compact(
                {"status": choice(last.get("status")), "at": last.get("completed") or last.get("created")}
            )
            if last
            else None,
        }
    )


def job_summary(job: dict[str, Any]) -> dict[str, Any]:
    raw = job.get("data")
    data: dict[str, Any] = raw if isinstance(raw, dict) else {}
    log = [f"{e.get('status', 'info')}: {e.get('message')}" for e in (data.get("log") or [])]
    out = {
        "job_id": job["id"],
        "name": job.get("name"),
        "object": label(job.get("object")),
        "status": choice(job.get("status")),
        "user": label(job.get("user")),
        "created": job.get("created"),
        "completed": job.get("completed"),
        "execution_time": job.get("execution_time"),
        "output": data.get("output"),
        "log": log[:MAX_LOG_LINES],
        "error": job.get("error"),
    }
    if len(log) > MAX_LOG_LINES:
        out["note"] = f"Log truncated to {MAX_LOG_LINES} of {len(log)} lines."
    return compact_dict(out)


async def resolve_script(nb: NetBoxClient, ref: str | int) -> dict[str, Any]:
    """A script by id, class name ('SetDescription'), or 'module.Class' / display text."""
    if isinstance(ref, int) or str(ref).isdigit():
        return await nb.get_object(f"extras/scripts/{int(ref)}/")
    text = str(ref).strip()
    module, _, cls = text.rpartition(".")
    name = cls or text
    found, _ = await nb.list("extras/scripts/", {"name": name}, limit=20)
    if module:
        found = [s for s in found if f"({module})" in (s.get("display") or "")]
    if not found:
        found = [
            s
            for s in (await nb.list("extras/scripts/", {}, limit=500))[0]
            if text.lower() in f"{s.get('display')} {s.get('description')}".lower()
        ]
    if len(found) == 1:
        return found[0]
    if not found:
        raise InvalidRequest(f"No script matching {ref!r}. Use netbox_list_scripts.")
    raise InvalidRequest(f"Script {ref!r} is ambiguous: {', '.join(s.get('display') or '?' for s in found)}.")


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def netbox_list_scripts(ctx: Context) -> dict[str, Any]:
        """NetBox custom scripts: name, module, description, inputs (name -> variable type) and the
        status of their last run. Run one with netbox_run_script (when writes are enabled)."""
        scripts, total = await get_netbox(ctx).list("extras/scripts/", {}, limit=500)
        return {"count": total, "scripts": [_script_row(s) for s in scripts]}

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_job_status(
        ctx: Context,
        job_id: Annotated[int | None, Field(description="A job id for full detail (output and log).")] = None,
        status: Annotated[
            str | None, Field(description="Filter recent jobs: pending, running, completed, errored, failed.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Background jobs (script runs, data syncs, ...): one job's status, output and log, or the
        most recent jobs."""
        nb = get_netbox(ctx)
        if job_id is not None:
            return job_summary(await nb.get_object(f"core/jobs/{job_id}/"))
        params: dict[str, Any] = {"ordering": "-created"}
        if status:
            params["status"] = status
        jobs, total = await nb.list("core/jobs/", params, limit=limit)
        return {
            "count": total,
            "jobs": [
                compact(
                    {
                        "job_id": j["id"],
                        "name": j.get("name"),
                        "object": label(j.get("object")),
                        "status": choice(j.get("status")),
                        "user": label(j.get("user")),
                        "created": j.get("created"),
                        "execution_time": j.get("execution_time"),
                    }
                )
                for j in jobs
            ],
        }

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_export(
        ctx: Context,
        object_type: Annotated[str, Field(description="Object type to export, e.g. 'devices'.")],
        template: Annotated[
            str | None, Field(description="Export template name. Omit to list templates for this type.")
        ] = None,
        filters: Annotated[
            dict[str, Any] | None,
            Field(description="Filters for the objects to export, as in netbox_list_objects."),
        ] = None,
    ) -> dict[str, Any]:
        """Render a NetBox export template (CSV, configs, inventories, ...) over a filtered set of
        objects and return the text. Without a template, lists the templates available for the type."""
        nb = get_netbox(ctx)
        schema = await get_schema(ctx)
        otype = schema.resolve_type(object_type)
        ct = content_type(otype)
        if template is None:
            templates, _ = await nb.list("extras/export-templates/", {}, limit=500)
            usable = [t for t in templates if ct in (t.get("object_types") or [])]
            return {
                "object_type": otype.key,
                "templates": [
                    compact({"name": t.get("name"), "description": t.get("description")}) for t in usable
                ],
            }
        filters = filters or {}
        cf = [n for n in filters if n.startswith("cf_")]
        schema.check_filters(otype, [n for n in filters if not n.startswith("cf_")])
        if cf:
            await check_custom_field_filters(nb, ct, cf)
        text, mime = await nb.get_text(otype.path, {**filters, "export": template})
        out: dict[str, Any] = {
            "object_type": otype.key,
            "template": template,
            "content_type": mime.split(";")[0],
            "lines": text.count("\n"),
            "content": text[:MAX_RESPONSE_CHARS],
        }
        if len(text) > MAX_RESPONSE_CHARS:
            out["note"] = f"Truncated to {MAX_RESPONSE_CHARS} of {len(text)} characters; narrow with filters."
        return out

    @mcp.tool(annotations=READ_ONLY)
    async def netbox_graphql(
        ctx: Context,
        query: Annotated[
            str,
            Field(
                description='A GraphQL query, e.g. \'{ device_list(filters: {name: {i_contains: "sw"}}) '
                "{ name interfaces { name } } }'. Read-only: mutations are rejected."
            ),
        ],
        variables: Annotated[dict[str, Any] | None, Field(description="Query variables.")] = None,
    ) -> dict[str, Any]:
        """Run a read-only GraphQL query against NetBox, for nested questions that would otherwise
        need many REST calls (e.g. devices with their interfaces and IPs in one request)."""
        if _MUTATION_RE.match(_strip_graphql_comments(query)):
            raise InvalidRequest(
                "Only read-only GraphQL queries are allowed (no mutations or subscriptions)."
            )
        result = await get_netbox(ctx).graphql(query, variables)
        out: dict[str, Any] = {}
        if result.get("errors"):
            out["errors"] = [e.get("message") for e in result["errors"]]
        data = result.get("data")
        text = str(data)
        if len(text) > MAX_RESPONSE_CHARS:
            out["note"] = "Result too large: request fewer fields or add filters / pagination."
            out["data_preview"] = text[:MAX_RESPONSE_CHARS]
        else:
            out["data"] = data
        return out

    if not settings.netbox_mcp_enable_writes:
        return

    @mcp.tool(annotations=RUN)
    async def netbox_run_script(
        ctx: Context,
        script: Annotated[
            str | int, Field(description="Script class name (e.g. 'SetDescription'), 'module.Class' or id.")
        ],
        data: Annotated[
            dict[str, Any] | None,
            Field(
                description="Script inputs by name (see netbox_list_scripts). "
                "Object inputs take the object id."
            ),
        ] = None,
        commit: Annotated[
            bool, Field(description="False (default) = dry run: NetBox rolls back all database changes.")
        ] = False,
        wait_seconds: Annotated[
            int,
            Field(
                description="How long to wait for the result (0 = return the job id at once).", ge=0, le=120
            ),
        ] = 30,
        confirm: Annotated[
            str | None,
            Field(description="Needed only with commit=true: omit to get a dry-run preview and a token."),
        ] = None,
    ) -> dict[str, Any]:
        """Run a NetBox custom script. Defaults to a dry run (commit=false: NetBox rolls back all changes).
        With commit=true the first call returns a dry-run preview and a confirm token; repeat it with
        confirm=<token> to run for real. Waits for the job and returns its status, output and log."""
        nb = get_netbox(ctx)
        s = await resolve_script(nb, script)
        data = data or {}
        unknown = sorted(set(data) - set(s.get("vars") or {}))
        if unknown:
            raise InvalidRequest(
                f"Unknown input(s) for {s.get('name')}: {unknown}. Inputs: {sorted(s.get('vars') or {})}."
            )
        if not s.get("is_executable", True):
            raise InvalidRequest(
                f"Script {s.get('name')} isn't executable (check the script module for errors)."
            )

        async def run(commit_run: bool) -> dict[str, Any]:
            started = await nb.request(
                "POST", f"extras/scripts/{s['id']}/", json={"data": data, "commit": commit_run}
            )
            job = started.get("result") or {}
            audit.info(
                "caller=%s run script=%s commit=%s job=%s",
                current_caller(),
                s.get("display"),
                commit_run,
                job.get("id"),
            )
            deadline = time.monotonic() + wait_seconds
            while choice(job.get("status")) not in FINISHED and time.monotonic() < deadline:
                await asyncio.sleep(1)
                job = await nb.get_object(f"core/jobs/{job['id']}/")
            out = {"script": s.get("display"), "commit": commit_run, **job_summary(job)}
            if choice(job.get("status")) not in FINISHED:
                out["note"] = f"Still running; check later with netbox_job_status(job_id={job.get('id')})."
            return out

        if not commit:
            return await run(False)  # a dry run changes nothing, so it needs no confirmation
        args = {"script": script, "data": data, "commit": commit}
        details: dict[str, Any] = {"script": s.get("display"), "inputs": data}
        if confirm is None:
            details["dry_run"] = await run(False)
        plan = Plan(summary=f"Run {s.get('display')} with commit=true", details=details)

        async def apply() -> dict[str, Any]:
            return await run(True)

        return await preview_or_apply(
            get_previews(ctx),
            tool="netbox_run_script",
            args=args,
            confirm=confirm,
            caller=current_caller(),
            plan=plan,
            apply=apply,
        )
