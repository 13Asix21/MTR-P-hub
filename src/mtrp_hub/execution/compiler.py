"""Expand immutable v1 logical routes into the complete stage 2 schedule."""

from __future__ import annotations

from typing import Any

from mtrp_hub.canonical import sha256_json
from mtrp_hub.config.stage2 import LoadedStage2Config, Stage2Mode
from mtrp_hub.domain.enums import LocationKind, ReadOperation, StoreKind
from mtrp_hub.domain.requests import ReadPlan, ReadStep, RouteQueryRequest
from mtrp_hub.errors import MtrpError, configuration_error
from mtrp_hub.execution.models import (
    AdapterKind,
    ExecutionLimits,
    ExecutionManifest,
    ExecutionMode,
    ExecutorKind,
    ManifestStep,
    Stage2Operation,
    Stage2RoutePlan,
)
from mtrp_hub.execution.profile import STAGE2_PROFILE, Stage2ReadonlyProfile
from mtrp_hub.security import RequestAccessGuard


class ManifestCompiler:
    """Compile only trusted Registry/configuration coordinates, never request paths."""

    def __init__(self, config: LoadedStage2Config, profile: Stage2ReadonlyProfile | None = None) -> None:
        self._config = config
        self._profile = profile or STAGE2_PROFILE

    def create_route(
        self,
        request: RouteQueryRequest,
        v1_plan: ReadPlan,
        mode: ExecutionMode | Stage2Mode,
    ) -> Stage2RoutePlan:
        del request  # The request is already cryptographically bound inside v1_plan.
        selected_mode = ExecutionMode(mode)
        has_remote = any(step.store in (StoreKind.P, StoreKind.M) for step in v1_plan.steps)
        has_core = any(step.store in (StoreKind.T, StoreKind.R) for step in v1_plan.steps)
        self._config.config.validate_requested_mode(selected_mode.value, has_remote=has_remote, has_core=has_core)
        values: dict[str, Any] = {
            "schema_version": "1",
            "mode": selected_mode,
            "v1_plan": v1_plan,
            "execution_config_hash": self._config.config.execution_config_hash,
            "profile_hash": self._profile.profile_hash(),
            "plan_hash": "0" * 64,
        }
        hashed_payload = dict(values)
        hashed_payload.pop("plan_hash")
        values["plan_hash"] = sha256_json(hashed_payload)
        return Stage2RoutePlan.model_validate(values)

    def compile(
        self,
        request: RouteQueryRequest,
        route: Stage2RoutePlan | ReadPlan,
        config: LoadedStage2Config | None = None,
        mode: ExecutionMode | Stage2Mode | None = None,
    ) -> ExecutionManifest:
        if config is not None and config is not self._config:
            self._config = config
        if isinstance(route, ReadPlan):
            if mode is None:
                raise MtrpError("MANIFEST_INVALID", "An explicit execution mode is required.", field="mode")
            route = self.create_route(request, route, mode)
        if route.v1_plan.request_hash != RequestAccessGuard.successful_request_hash(request):
            raise MtrpError("MANIFEST_CONTEXT_CHANGED", "The request differs from the routed plan.", field="request")
        steps: list[ManifestStep] = []
        for logical_step in route.steps:
            if logical_step.store is StoreKind.P:
                steps.extend(self.expand_p(logical_step))
            elif logical_step.store is StoreKind.M:
                steps.extend(self.expand_m(logical_step, request.query))
            else:
                steps.append(self.expand_view(logical_step, request.query))
        if not steps or len(steps) > self._config.workbuddy_manifest.max_steps:
            raise MtrpError("MANIFEST_STEP_LIMIT_EXCEEDED", "The expanded schedule exceeds the configured step limit.", field="steps")
        max_tool_calls = max(1, self._maximum_tool_calls(route))
        values: dict[str, Any] = {
            "schema_version": "1",
            "mode": route.mode,
            "read_only": True,
            "request_id": route.request_id,
            "route_id": route.route_id,
            "execution_id": "exe_" + "0" * 64,
            "request_hash": route.v1_plan.request_hash,
            "plan_hash": route.plan_hash,
            "manifest_hash": "0" * 64,
            "tool_profile": self._profile.name,
            "profile_hash": route.profile_hash,
            "execution_config_hash": route.execution_config_hash,
            "limits": ExecutionLimits(
                max_steps=self._config.workbuddy_manifest.max_steps,
                max_entry_reads=self._config.workbuddy_manifest.max_entry_reads,
                max_tool_calls=max_tool_calls,
                max_file_bytes=self._config.github_p.max_file_bytes,
                max_entry_bytes=self._config.lexiang_m.max_entry_bytes,
                max_envelope_bytes=self._config.workbuddy_manifest.max_envelope_bytes,
                max_scan_files=self._config.obsidian.max_scan_files,
                max_results=self._config.obsidian.max_results,
            ),
            "steps": tuple(steps),
        }
        hashed_payload = dict(values)
        hashed_payload.pop("execution_id")
        hashed_payload.pop("manifest_hash")
        manifest_hash = sha256_json(hashed_payload)
        values["manifest_hash"] = manifest_hash
        values["execution_id"] = f"exe_{manifest_hash}"
        return ExecutionManifest.model_validate(values)

    def expand_p(self, step: ReadStep) -> tuple[ManifestStep, ...]:
        coordinate = step.coordinate
        if coordinate is None or coordinate.location_kind is not LocationKind.GITHUB or coordinate.workspace_id is not None:
            raise configuration_error("GIT_REGISTRY_INVALID", "projects", "Stage 2 P requires an authoritative GitHub Location.")
        binding = self._config.github_p.binding_for(coordinate)
        common = {"owner": binding.owner, "repo": binding.repo}
        prefix = step.step_id
        resolve_id = f"{prefix}-02-resolve-ref"
        result = [
            self._remote_step(f"{prefix}-01-list-branches", step, AdapterKind.GITHUB_P, Stage2Operation.GITHUB_LIST_BRANCHES, dict(common)),
            self._remote_step(resolve_id, step, AdapterKind.GITHUB_P, Stage2Operation.GITHUB_RESOLVE_REF, {**common, "ref": coordinate.ref}),
            self._remote_step(
                f"{prefix}-03-read-repository-root",
                step,
                AdapterKind.GITHUB_P,
                Stage2Operation.GITHUB_READ_REPOSITORY_ROOT,
                {**common, "sha": {"from_step": resolve_id, "field": "commit"}},
            ),
        ]
        paths = binding.adr_files if step.operation is ReadOperation.ADR_SEARCH else binding.read_paths
        for index, path in enumerate(paths, start=1):
            result.append(
                self._remote_step(
                    f"{prefix}-{index + 3:02d}-read-file-{index:02d}",
                    step,
                    AdapterKind.GITHUB_P,
                    Stage2Operation.GITHUB_READ_FILE,
                    {**common, "path": path, "sha": {"from_step": resolve_id, "field": "commit"}},
                )
            )
        return tuple(result)

    def expand_m(self, step: ReadStep, query: str) -> tuple[ManifestStep, ...]:
        config = self._config.lexiang_m
        result = [
            self._remote_step(
                f"{step.step_id}-01-resolve-space",
                step,
                AdapterKind.LEXIANG_M,
                Stage2Operation.LEXIANG_RESOLVE_SPACE,
                {"space_id": config.space_id, "team_id": config.team_id, "root_entry_id": config.root_entry_id},
            )
        ]
        sequence = 2
        for search_mode in ("text", "vector"):
            if search_mode not in config.search_modes:
                continue
            search_operation = Stage2Operation.LEXIANG_SEARCH_TEXT if search_mode == "text" else Stage2Operation.LEXIANG_SEARCH_VECTOR
            search_id = f"{step.step_id}-{sequence:02d}-search-{search_mode}"
            arguments: dict[str, Any]
            if search_mode == "text":
                arguments = {"space_id": config.space_id, "keyword": query, "limit": config.max_results}
            else:
                arguments = {
                    "space_id": config.space_id,
                    "filters": {"keyword": query},
                    "limit": config.max_results,
                    "threshold": config.vector_threshold,
                }
            result.append(self._remote_step(search_id, step, AdapterKind.LEXIANG_M, search_operation, arguments))
            sequence += 1
            result.append(
                self._remote_step(
                    f"{step.step_id}-{sequence:02d}-read-{search_mode}-entries",
                    step,
                    AdapterKind.LEXIANG_M,
                    Stage2Operation.LEXIANG_READ_ENTRIES,
                    {"entry_reads": {"from_step": search_id, "field": "entry_reads"}},
                )
            )
            sequence += 1
        return tuple(result)

    def expand_view(self, step: ReadStep, query: str) -> ManifestStep:
        if step.view_tag is None:
            raise MtrpError("MANIFEST_INVALID", "A Core Vault view requires its fixed tag.", field=step.step_id)
        return ManifestStep(
            step_id=f"{step.step_id}-01-vault-view-search",
            route_step_id=step.step_id,
            executor=ExecutorKind.CORE,
            adapter=AdapterKind.OBSIDIAN_LOCAL,
            operation=Stage2Operation.VAULT_VIEW_SEARCH,
            arguments={
                "vault_id": self._config.obsidian.vault_id,
                "view_tag": step.view_tag,
                "query": query,
                "scope_sha256": self._config.obsidian.scope_sha256,
            },
            on_empty=step.on_empty,
            on_unavailable=step.on_unavailable,
            read_only=True,
        )

    @staticmethod
    def _remote_step(
        step_id: str,
        logical_step: ReadStep,
        adapter: AdapterKind,
        operation: Stage2Operation,
        arguments: dict[str, Any],
    ) -> ManifestStep:
        return ManifestStep(
            step_id=step_id,
            route_step_id=logical_step.step_id,
            executor=ExecutorKind.WORKBUDDY,
            adapter=adapter,
            operation=operation,
            arguments=arguments,
            on_empty=logical_step.on_empty,
            on_unavailable=logical_step.on_unavailable,
            read_only=True,
        )

    def _maximum_tool_calls(self, route: Stage2RoutePlan) -> int:
        total = 0
        for logical in route.steps:
            if logical.store is StoreKind.P:
                coordinate = logical.coordinate
                if coordinate is None:
                    continue
                binding = self._config.github_p.binding_for(coordinate)
                paths = binding.adr_files if logical.operation is ReadOperation.ADR_SEARCH else binding.read_paths
                total += 3 + len(paths)
            elif logical.store is StoreKind.M:
                total += 1 + len(self._config.lexiang_m.search_modes) * (1 + 2 * self._config.lexiang_m.max_results)
        return total


__all__ = ["ManifestCompiler"]
