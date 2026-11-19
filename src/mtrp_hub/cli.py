"""Typer command tree, deterministic presenters, and safe exit-code mapping."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NoReturn

import typer
from pydantic import ValidationError

from mtrp_hub.application import RouteQueryService
from mtrp_hub.audit.models import DecisionEventType, DecisionPayload, DecisionRecord
from mtrp_hub.audit.sqlite import SQLiteDecisionLog
from mtrp_hub.canonical import canonical_json, sha256_json
from mtrp_hub.config.loader import ConfigLoader
from mtrp_hub.domain.enums import Intent, OutputFormat, StoreKind
from mtrp_hub.domain.requests import RouteQueryRequest, RouteQueryResponse
from mtrp_hub.errors import MtrpError
from mtrp_hub.security import RequestAccessGuard, rejected_write_command

app = typer.Typer(
    name="mtrp",
    help="Deterministic MTR-P read-only routing with Fake adapters only.",
    no_args_is_help=True,
    add_completion=False,
)
route_app = typer.Typer(help="Generate and inspect read-only route decisions.", no_args_is_help=True)
app.add_typer(route_app, name="route")


def response_payload(response: RouteQueryResponse) -> dict[str, object]:
    """Flatten ReadPlan fields into the stable public JSON response shape."""
    payload = response.plan.model_dump(mode="json")
    payload.update(
        {
            "step_results": [item.model_dump(mode="json") for item in response.step_results],
            "fixture": {"fixture_id": response.fixture_id, "fixture_hash": response.fixture_hash},
            "resources": [item.model_dump(mode="json") for item in response.result.resources],
            "relations": [item.model_dump(mode="json") for item in response.result.relations],
            "evidence": [item.model_dump(mode="json") for item in response.result.evidence],
            "write_proposal": None,
        }
    )
    return payload


def render_text(response: RouteQueryResponse) -> str:
    """Render deterministic human-readable route and degradation information."""
    route_parts = []
    for step in response.plan.steps:
        label = step.store.value
        if step.store in (StoreKind.T, StoreKind.R):
            label = f"{label}[{step.view_tag}]"
        route_parts.append(label)
    lines = [
        f"规则版本: {response.plan.rule_version}",
        f"只读计划: {' -> '.join(route_parts)}",
        "步骤状态: " + "; ".join(
            f"{item.step_id}={item.status.value}:{item.reason_code.value if item.reason_code else 'NONE'}"
            for item in response.step_results
        ),
        "空结果策略: " + " -> ".join(step.on_empty.value for step in response.plan.steps),
        "外部写入: disabled",
    ]
    return "\n".join(lines) + "\n"


def _safe_error(error: MtrpError) -> None:
    sys.stderr.write(canonical_json(error.as_safe_dict()) + "\n")
    raise typer.Exit(code=error.exit_code)


def _request_validation_error(error: ValidationError) -> MtrpError:
    """Map request DTO failures to a safe field without echoing invalid input."""
    allowed_fields = {
        "query",
        "intent",
        "project_id",
        "repo_id",
        "workspace_id",
        "config_dir",
        "fixture",
        "include_r",
        "format",
    }
    field = next(
        (
            str(location[0])
            for item in error.errors(include_url=False, include_input=False)
            if (location := item.get("loc", ())) and str(location[0]) in allowed_fields
        ),
        None,
    )
    return MtrpError("INVALID_INPUT", "Request field validation failed.", field=field)


@route_app.command("query")
def route_query(
    query: str = typer.Option(..., "--query", help="Question text; hashed but never fetched as a URL."),
    intent: Intent = typer.Option(..., "--intent", help="One of the five versioned routing intents."),
    project: str | None = typer.Option(None, "--project", help="Registered Project ID."),
    repo: str | None = typer.Option(None, "--repo", help="Registered Repository ID."),
    workspace: str | None = typer.Option(None, "--workspace", help="Registered Workspace ID (query disabled in this stage)."),
    config_dir: str = typer.Option(..., "--config-dir", help="Explicit directory containing the three YAML files."),
    fixture: str | None = typer.Option(None, "--fixture", help="Optional cwd-relative JSON Fixture without '..'."),
    include_r: bool = typer.Option(False, "--include-r", help="Append mtr/rarely to an ordinary intent."),
    output_format: OutputFormat = typer.Option(OutputFormat.JSON, "--format", help="Stable JSON or concise text output."),
) -> None:
    """Generate a ReadPlan and execute it against in-memory Fake data only."""
    try:
        request = RouteQueryRequest(
            query=query,
            intent=intent,
            project_id=project,
            repo_id=repo,
            workspace_id=workspace,
            config_dir=config_dir,
            fixture=fixture,
            include_r=include_r,
            format=output_format,
        )
        response = RouteQueryService().query(request)
    except ValidationError as exc:
        _safe_error(_request_validation_error(exc))
        return
    except MtrpError as exc:
        _safe_error(exc)
        return
    if output_format is OutputFormat.JSON:
        sys.stdout.write(canonical_json(response_payload(response)) + "\n")
    else:
        sys.stdout.write(render_text(response))


@route_app.command("decisions")
def route_decisions(
    config_dir: str = typer.Option(..., "--config-dir", help="Explicit directory containing .mtrp Decision Log."),
    request_hash: str | None = typer.Option(None, "--request-hash", help="Full request SHA-256."),
    plan_hash: str | None = typer.Option(None, "--plan-hash", help="Full plan SHA-256."),
    output_format: OutputFormat = typer.Option(OutputFormat.JSON, "--format", help="JSON or text output."),
) -> None:
    """Inspect only the local whitelist Decision Log by request or plan hash."""
    try:
        log = SQLiteDecisionLog(Path(config_dir) / ".mtrp" / "decision-log.sqlite3", create=False)
        records = log.find(request_hash=request_hash, plan_hash=plan_hash)
    except MtrpError as exc:
        _safe_error(exc)
        return
    if output_format is OutputFormat.JSON:
        sys.stdout.write(canonical_json([record.model_dump(mode="json") for record in records]) + "\n")
    else:
        for record in records:
            sys.stdout.write(
                f"{record.event_type.value} {record.status} {record.reason_code or '-'} {record.request_hash}\n"
            )


def _exit_main(error: MtrpError) -> NoReturn:
    """Emit a pre-dispatch safe error without Typer/Rich traceback rendering."""
    sys.stderr.write(canonical_json(error.as_safe_dict()) + "\n")
    raise SystemExit(error.exit_code)


def _write_config_dir(arguments: list[str]) -> str:
    """Extract exactly one explicit bootstrap directory from a rejected command."""
    values: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--config-dir":
            if index + 1 >= len(arguments):
                raise MtrpError("INVALID_INPUT", "Write rejection requires --config-dir.", field="config_dir")
            values.append(arguments[index + 1])
            index += 2
            continue
        if argument.startswith("--config-dir="):
            values.append(argument.split("=", 1)[1])
        index += 1
    if len(values) != 1 or not values[0] or values[0].startswith("--"):
        raise MtrpError(
            "INVALID_INPUT",
            "Write rejection requires exactly one valid --config-dir.",
            field="config_dir",
        )
    return values[0]


def _record_write_rejection(write_command: str, arguments: list[str]) -> None:
    """Validate explicit bootstrap config, then append one sanitized rejection."""
    config_dir = _write_config_dir(arguments)
    try:
        bootstrap = RouteQueryRequest(
            query="disabled write request",
            intent=Intent.GENERAL_KNOWLEDGE,
            config_dir=config_dir,
        )
    except ValidationError as exc:
        raise _request_validation_error(exc) from exc
    RequestAccessGuard().validate_bootstrap(bootstrap)
    ConfigLoader().load(config_dir)
    request_hash = sha256_json({"decision": "WRITE_DISABLED", "command": write_command})
    record = DecisionRecord(
        event_type=DecisionEventType.REQUEST_REJECTED,
        request_hash=request_hash,
        plan_hash=None,
        status="rejected",
        reason_code="WRITE_DISABLED",
        payload=DecisionPayload(
            error_code="WRITE_DISABLED",
            field=write_command,
            final_status="rejected",
        ),
    )
    SQLiteDecisionLog(Path(config_dir) / ".mtrp" / "decision-log.sqlite3").append(record)


def main() -> None:
    """Shared console/module entry point with audited pre-dispatch write rejection."""
    write_command = rejected_write_command(sys.argv[1:])
    if write_command is not None:
        try:
            _record_write_rejection(write_command, sys.argv[1:])
        except MtrpError as error:
            _exit_main(error)
        _exit_main(MtrpError("WRITE_DISABLED", "External write commands are disabled.", field=write_command))
    app()


__all__ = ["app", "main", "render_text", "response_payload", "route_app"]
