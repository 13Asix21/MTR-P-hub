"""Static and runtime resolution of the only permitted prior-step bindings."""

from __future__ import annotations

import copy
from typing import Any, Iterable

from pydantic import ValidationError

from mtrp_hub.domain.models import FrozenModel, validate_commit
from mtrp_hub.errors import MtrpError
from mtrp_hub.execution.models import (
    AdapterKind,
    EntryCandidate,
    ExecutionManifest,
    ManifestStep,
    PlannedToolCall,
    Stage2Operation,
    StepBinding,
)
from mtrp_hub.execution.profile import STAGE2_PROFILE, Stage2ReadonlyProfile


class ResolvedCalls(FrozenModel):
    calls: tuple[PlannedToolCall, ...]
    arguments: dict[str, Any]
    candidate_count: int = 0


def upstream_blocks(step: ManifestStep, prior_receipts: Iterable[Any]) -> bool:
    """Apply the same serial dependency barriers in executor and verifier."""
    same_route = [receipt for receipt in prior_receipts if receipt.route_step_id == step.route_step_id]
    if step.adapter is AdapterKind.GITHUB_P and step.operation is not Stage2Operation.GITHUB_LIST_BRANCHES:
        branches = next((receipt for receipt in same_route if receipt.step_id.endswith("list-branches")), None)
        if branches is not None and branches.status.value != "OK":
            return True
    if step.adapter is AdapterKind.LEXIANG_M and step.operation is not Stage2Operation.LEXIANG_RESOLVE_SPACE:
        space = next((receipt for receipt in same_route if receipt.step_id.endswith("resolve-space")), None)
        if space is not None and space.status.value != "OK":
            return True
    return False


class BindingResolver:
    """Resolve declared fields only; never substitute a branch latest fallback."""

    def __init__(
        self,
        profile: Stage2ReadonlyProfile | None = None,
        *,
        max_entry_reads: int = 10,
        lexiang_timeout_seconds: int = 10,
        github_timeout_seconds: int = 10,
    ) -> None:
        self._profile = profile or STAGE2_PROFILE
        self._max_entry_reads = max_entry_reads
        self._lexiang_timeout = lexiang_timeout_seconds
        self._github_timeout = github_timeout_seconds

    def validate_manifest(self, manifest: ExecutionManifest) -> None:
        positions = {step.step_id: index for index, step in enumerate(manifest.steps)}
        by_id = {step.step_id: step for step in manifest.steps}
        entry_read_budget = 0
        for step in manifest.steps:
            self._profile.validate_arguments(step)
            for argument_name, binding in self._bindings(step):
                source = by_id.get(binding.from_step)
                if source is None or positions[binding.from_step] >= positions[step.step_id]:
                    raise MtrpError("BINDING_NOT_EARLIER", "A binding must reference an existing earlier step.", field=step.step_id)
                if binding.field == "commit":
                    if source.operation is not Stage2Operation.GITHUB_RESOLVE_REF or step.operation not in (
                        Stage2Operation.GITHUB_READ_REPOSITORY_ROOT,
                        Stage2Operation.GITHUB_READ_FILE,
                    ):
                        raise MtrpError("BINDING_FIELD_FORBIDDEN", "The commit binding source or consumer is forbidden.", field=step.step_id)
                elif binding.field == "entry_reads":
                    if source.operation not in (Stage2Operation.LEXIANG_SEARCH_TEXT, Stage2Operation.LEXIANG_SEARCH_VECTOR) or step.operation is not Stage2Operation.LEXIANG_READ_ENTRIES:
                        raise MtrpError("BINDING_FIELD_FORBIDDEN", "The entry binding source or consumer is forbidden.", field=step.step_id)
                    entry_read_budget += source.arguments["limit"]
                else:
                    raise MtrpError("BINDING_FIELD_FORBIDDEN", "The binding field is not declared by the profile.", field=step.step_id)
                if argument_name not in step.arguments:
                    raise MtrpError("BINDING_INVALID", "The binding location is invalid.", field=step.step_id)
        if entry_read_budget > min(manifest.limits.max_entry_reads, self._max_entry_reads):
            raise MtrpError("ENTRY_READ_LIMIT_EXCEEDED", "Declared searches exceed the entry read budget.", field="limits")

    @staticmethod
    def _bindings(step: ManifestStep) -> tuple[tuple[str, StepBinding], ...]:
        result: list[tuple[str, StepBinding]] = []
        for key, value in step.arguments.items():
            if isinstance(value, dict) and set(value) == {"from_step", "field"}:
                try:
                    result.append((key, StepBinding.model_validate(value)))
                except ValidationError as exc:
                    raise MtrpError("BINDING_INVALID", "A Manifest binding is invalid.", field=step.step_id) from exc
            elif isinstance(value, dict) and ("from_step" in value or "field" in value):
                raise MtrpError("BINDING_INVALID", "Arbitrary binding expressions are forbidden.", field=step.step_id)
        return tuple(result)

    def resolve(self, step: ManifestStep, prior_receipts: Iterable[Any]) -> ResolvedCalls:
        receipts = tuple(prior_receipts)
        by_id = {receipt.step_id: receipt for receipt in receipts}
        arguments = copy.deepcopy(step.arguments)
        candidate_count = 0
        for argument_name, binding in self._bindings(step):
            source = by_id.get(binding.from_step)
            if source is None:
                raise MtrpError("UPSTREAM_BINDING_FAILED", "An upstream binding receipt is unavailable.", field=step.step_id)
            if getattr(source, "status", None) is not None and source.status.value != "OK":
                if binding.field == "entry_reads" and source.status.value == "EMPTY" and source.binding_data.get("entry_reads") == []:
                    arguments[argument_name] = []
                    continue
                raise MtrpError("UPSTREAM_BINDING_FAILED", "An upstream step did not produce a usable binding.", field=step.step_id)
            if binding.field not in source.binding_data:
                raise MtrpError("UPSTREAM_BINDING_FAILED", "The upstream binding field is unavailable.", field=step.step_id)
            value = source.binding_data[binding.field]
            if binding.field == "commit":
                arguments[argument_name] = self.scalar_commit(value, step)
            else:
                candidates = self.entry_candidates(value, step)
                candidate_count = len(candidates)
                arguments[argument_name] = candidates
        calls = self._profile.materialize_calls(step, arguments)
        timeout = self._lexiang_timeout if step.adapter is AdapterKind.LEXIANG_M else self._github_timeout
        calls = tuple(call.model_copy(update={"timeout_seconds": timeout}) for call in calls)
        serializable_arguments = {
            key: [item.model_dump(mode="json") for item in value] if key == "entry_reads" and isinstance(value, list) else value
            for key, value in arguments.items()
        }
        return ResolvedCalls(calls=calls, arguments=serializable_arguments, candidate_count=candidate_count)

    @staticmethod
    def scalar_commit(value: Any, step: ManifestStep | None = None) -> str:
        field = step.step_id if step else "binding"
        if value is None or value == "":
            raise MtrpError("BINDING_EMPTY", "A required commit binding is empty.", field=field)
        if not isinstance(value, str):
            raise MtrpError("BINDING_TYPE_INVALID", "A commit binding must be a full SHA string.", field=field)
        try:
            validate_commit(value)
        except ValueError as exc:
            raise MtrpError("BINDING_TYPE_INVALID", "A commit binding must be a full lowercase SHA-1.", field=field) from exc
        return value

    def entry_candidates(self, value: Any, step: ManifestStep | None = None) -> list[EntryCandidate]:
        field = step.step_id if step else "binding"
        if not isinstance(value, list):
            raise MtrpError("BINDING_TYPE_INVALID", "Entry bindings must be a candidate list.", field=field)
        if len(value) > self._max_entry_reads:
            raise MtrpError("ENTRY_READ_LIMIT_EXCEEDED", "The candidate list exceeds the entry read budget.", field=field)
        result: list[EntryCandidate] = []
        seen: set[str] = set()
        for raw in value:
            try:
                candidate = raw if isinstance(raw, EntryCandidate) else EntryCandidate.model_validate(raw)
            except ValidationError as exc:
                raise MtrpError("BINDING_TYPE_INVALID", "An entry candidate is invalid.", field=field) from exc
            if candidate.entry_id in seen:
                continue
            seen.add(candidate.entry_id)
            result.append(candidate)
        return result


__all__ = ["BindingResolver", "ResolvedCalls", "upstream_blocks"]
