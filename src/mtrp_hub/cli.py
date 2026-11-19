"""Typer command tree, deterministic presenters, and safe exit-code mapping."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NoReturn

import typer
from pydantic import ValidationError

from mtrp_hub.application import RouteQueryService
from mtrp_hub.application_stage2 import ManifestService, QueryFinalizationService
from mtrp_hub.audit.models import DecisionEventType, DecisionPayload, DecisionRecord
from mtrp_hub.audit.sqlite import SQLiteDecisionLog
from mtrp_hub.audit.sqlite_stage2 import Stage2DecisionLog
from mtrp_hub.canonical import canonical_json, sha256_json
from mtrp_hub.config.loader import ConfigLoader
from mtrp_hub.config.stage2 import Stage2ConfigLoader
from mtrp_hub.domain.enums import Intent, OutputFormat, StoreKind
from mtrp_hub.domain.requests import RouteQueryRequest, RouteQueryResponse
from mtrp_hub.domain.requests_v2 import QueryResponseV2, response_payload_v2
from mtrp_hub.errors import MtrpError, internal_error
from mtrp_hub.execution.models import ExecutionMode
from mtrp_hub.execution.verifier import ManifestVerifier
from mtrp_hub.fixtures.stage2_loader import Stage2FixtureLoader
from mtrp_hub.security import RequestAccessGuard, rejected_write_command

app = typer.Typer(
    name="mtrp",
    help="Deterministic MTR-P read-only routing and controlled execution.",
    no_args_is_help=True,
    add_completion=False,
)
route_app = typer.Typer(help="Generate and inspect read-only route decisions.", no_args_is_help=True)
manifest_app = typer.Typer(help="Create and verify immutable read-only execution plans.", no_args_is_help=True)
query_app = typer.Typer(help="Finalize controlled read-only execution results.", no_args_is_help=True)
app.add_typer(route_app, name="route")
app.add_typer(manifest_app, name="manifest")
app.add_typer(query_app, name="query")


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


def _stage2_request(
    *,
    query: str,
    intent: Intent,
    project: str | None,
    repo: str | None,
    config_dir: str,
    include_r: bool,
    fixture: str | None = None,
) -> RouteQueryRequest:
    return RouteQueryRequest(
        query=query,
        intent=intent,
        project_id=project,
        repo_id=repo,
        workspace_id=None,
        config_dir=config_dir,
        fixture=fixture,
        include_r=include_r,
        format=OutputFormat.JSON,
    )


def _read_stdin_limited(max_bytes: int) -> str:
    """Read bounded controlled handoff bytes without creating a response file."""
    binary = getattr(sys.stdin, "buffer", None)
    if binary is not None:
        raw = binary.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise MtrpError("ENVELOPE_TOO_LARGE", "Controlled stdin exceeds its size limit.", field="envelope")
        try:
            return raw.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise MtrpError("ENVELOPE_INVALID", "Controlled stdin must be valid UTF-8.", field="envelope") from exc
    text = sys.stdin.read(max_bytes + 1)
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        raise MtrpError("ENVELOPE_TOO_LARGE", "Controlled stdin exceeds its size limit.", field="envelope")
    return text


def _stage2_input_limit(request: RouteQueryRequest) -> int:
    RequestAccessGuard().validate_bootstrap(request)
    return Stage2ConfigLoader().load(request.config_dir).workbuddy_manifest.max_envelope_bytes


def _render_v2_text(response: QueryResponseV2) -> str:
    lines = [
        f"模式: {response.data_mode.value}",
        "步骤状态: " + "; ".join(
            f"{step.step_id}={step.status.value}:{step.reason_code or 'NONE'}:{step.resource_count}"
            for step in response.step_results
        ),
        f"资源/证据: {len(response.result.resources)}/{len(response.result.evidence)}",
        f"追踪: {response.trace_id}",
        "外部写入: disabled",
    ]
    return "\n".join(lines) + "\n"


@manifest_app.command("create")
def manifest_create(
    mode: ExecutionMode = typer.Option(..., "--mode", help="Explicit controlled execution mode."),
    query: str = typer.Option(..., "--query"),
    intent: Intent = typer.Option(..., "--intent"),
    project: str | None = typer.Option(None, "--project"),
    repo: str | None = typer.Option(None, "--repo"),
    config_dir: str = typer.Option(..., "--config-dir"),
    include_r: bool = typer.Option(False, "--include-r"),
    output_format: OutputFormat = typer.Option(OutputFormat.JSON, "--format"),
) -> None:
    """Compile a Manifest without target Vault I/O or connector calls."""
    try:
        request = _stage2_request(query=query, intent=intent, project=project, repo=repo, config_dir=config_dir, include_r=include_r)
        manifest = ManifestService().create(request, mode)
    except ValidationError as exc:
        _safe_error(_request_validation_error(exc))
        return
    except MtrpError as exc:
        _safe_error(exc)
        return
    except Exception:
        _safe_error(internal_error())
        return
    if output_format is OutputFormat.JSON:
        sys.stdout.write(canonical_json(manifest) + "\n")
    else:
        sys.stdout.write(f"request_id={manifest.request_id}\nroute_id={manifest.route_id}\nexecution_id={manifest.execution_id}\nmanifest_hash={manifest.manifest_hash}\n")


@manifest_app.command("verify")
def manifest_verify(
    mode: ExecutionMode = typer.Option(..., "--mode"),
    query: str = typer.Option(..., "--query"),
    intent: Intent = typer.Option(..., "--intent"),
    project: str | None = typer.Option(None, "--project"),
    repo: str | None = typer.Option(None, "--repo"),
    config_dir: str = typer.Option(..., "--config-dir"),
    include_r: bool = typer.Option(False, "--include-r"),
) -> None:
    """Recompile from the original request/config and compare every field."""
    try:
        request = _stage2_request(query=query, intent=intent, project=project, repo=repo, config_dir=config_dir, include_r=include_r)
        maximum = _stage2_input_limit(request)
        supplied = Stage2FixtureLoader().load_manifest(_read_stdin_limited(maximum))
        ManifestService().verify(request, supplied, mode)
    except ValidationError as exc:
        _safe_error(_request_validation_error(exc))
        return
    except MtrpError as exc:
        _safe_error(exc)
        return
    except Exception:
        _safe_error(internal_error())
        return
    sys.stdout.write(canonical_json({"status": "VERIFIED", "request_id": supplied.request_id, "route_id": supplied.route_id, "execution_id": supplied.execution_id, "manifest_hash": supplied.manifest_hash, "external_connectivity": "not-tested"}) + "\n")


@query_app.command("fixture")
def query_fixture(
    query: str = typer.Option(..., "--query"),
    intent: Intent = typer.Option(..., "--intent"),
    project: str | None = typer.Option(None, "--project"),
    repo: str | None = typer.Option(None, "--repo"),
    config_dir: str = typer.Option(..., "--config-dir"),
    fixture: str = typer.Option(..., "--fixture"),
    include_r: bool = typer.Option(False, "--include-r"),
    output_format: OutputFormat = typer.Option(OutputFormat.JSON, "--format"),
) -> None:
    """Run the v2 state machine entirely with synthetic offline Fixtures."""
    try:
        request = _stage2_request(query=query, intent=intent, project=project, repo=repo, config_dir=config_dir, include_r=include_r, fixture=fixture)
        maximum = _stage2_input_limit(request)
        bundle = Stage2FixtureLoader().load(fixture, max_bytes=maximum)
        response = QueryFinalizationService().run_fixture(request, bundle)
    except ValidationError as exc:
        _safe_error(_request_validation_error(exc))
        return
    except MtrpError as exc:
        _safe_error(exc)
        return
    except Exception:
        _safe_error(internal_error())
        return
    if output_format is OutputFormat.JSON:
        sys.stdout.write(canonical_json(response_payload_v2(response)) + "\n")
    else:
        sys.stdout.write(_render_v2_text(response))


@query_app.command("finalize")
def query_finalize(
    mode: ExecutionMode = typer.Option(..., "--mode"),
    query: str = typer.Option(..., "--query"),
    intent: Intent = typer.Option(..., "--intent"),
    project: str | None = typer.Option(None, "--project"),
    repo: str | None = typer.Option(None, "--repo"),
    config_dir: str = typer.Option(..., "--config-dir"),
    include_r: bool = typer.Option(False, "--include-r"),
    expected_manifest_hash: str = typer.Option(..., "--expected-manifest-hash"),
) -> None:
    """Verify a controlled envelope, process Core barriers, and emit v2/continuation."""
    try:
        request = _stage2_request(query=query, intent=intent, project=project, repo=repo, config_dir=config_dir, include_r=include_r)
        maximum = _stage2_input_limit(request)
        envelope = Stage2FixtureLoader().load_envelope(_read_stdin_limited(maximum))
        output = QueryFinalizationService().finalize(request, expected_manifest_hash, envelope, mode)
    except ValidationError as exc:
        _safe_error(_request_validation_error(exc))
        return
    except MtrpError as exc:
        _safe_error(exc)
        return
    except Exception:
        _safe_error(internal_error())
        return
    if isinstance(output, QueryResponseV2):
        sys.stdout.write(canonical_json(response_payload_v2(output)) + "\n")
    else:
        sys.stdout.write(canonical_json(output) + "\n")


@route_app.command("traces")
def route_traces(
    config_dir: str = typer.Option(..., "--config-dir"),
    request_id: str | None = typer.Option(None, "--request-id"),
    route_id: str | None = typer.Option(None, "--route-id"),
    execution_id: str | None = typer.Option(None, "--execution-id"),
    trace_id: str | None = typer.Option(None, "--trace-id"),
) -> None:
    """Read safe stage 2 runs/events by exactly one full-chain identity."""
    try:
        loaded = Stage2ConfigLoader().load(config_dir)
        records = Stage2DecisionLog(loaded.audit_path, create=False).find(
            request_id=request_id,
            route_id=route_id,
            execution_id=execution_id,
            trace_id=trace_id,
        )
    except MtrpError as exc:
        _safe_error(exc)
        return
    except Exception:
        _safe_error(internal_error("DECISION_LOG_READ_FAILED"))
        return
    sys.stdout.write(canonical_json(records) + "\n")


@manifest_app.command("diff")
def manifest_diff(
    left: str = typer.Option(..., "--left"),
    right: str = typer.Option(..., "--right"),
) -> None:
    """Compare only synthetic Fixture Manifests; never execute their steps."""
    try:
        loader = Stage2FixtureLoader()
        from mtrp_hub.execution.models import ExecutionManifest

        left_manifest = loader.load_model(left, model=ExecutionManifest, field="manifest")
        right_manifest = loader.load_model(right, model=ExecutionManifest, field="manifest")
        if left_manifest.mode is not ExecutionMode.FIXTURE or right_manifest.mode is not ExecutionMode.FIXTURE:
            raise MtrpError("MANIFEST_INVALID", "Manifest diff only accepts Fixture Manifests.", field="mode")
        verifier = ManifestVerifier()
        verifier.verify_intrinsic(left_manifest)
        verifier.verify_intrinsic(right_manifest)
        changes: list[dict[str, str | None]] = []

        def visit(before, after, path: str, step_id: str | None = None) -> None:  # type: ignore[no-untyped-def]
            if isinstance(before, dict) and isinstance(after, dict) and set(before) == set(after):
                current_step = before.get("step_id", step_id) if path == "manifest.steps" or ".steps." in path else step_id
                for key in sorted(before):
                    visit(before[key], after[key], f"{path}.{key}", current_step if isinstance(current_step, str) else step_id)
                return
            if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
                for index, (left_item, right_item) in enumerate(zip(before, after, strict=True)):
                    visit(left_item, right_item, f"{path}.{index}", step_id)
                return
            if before != after:
                changes.append({"path": path, "step_id": step_id, "before_sha256": sha256_json(before), "after_sha256": sha256_json(after)})

        visit(left_manifest.model_dump(mode="json"), right_manifest.model_dump(mode="json"), "manifest")
    except MtrpError as exc:
        _safe_error(exc)
        return
    except Exception:
        _safe_error(internal_error())
        return
    sys.stdout.write(canonical_json({"changes": changes, "left_manifest_hash": left_manifest.manifest_hash, "right_manifest_hash": right_manifest.manifest_hash, "executed": False}) + "\n")


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
