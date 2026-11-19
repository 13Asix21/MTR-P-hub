"""In-memory P/M/T/R Fake adapters with honest empty/degraded outcomes."""

from __future__ import annotations

from collections.abc import Iterable

from mtrp_hub.adapters.base import FakeExecutionContext
from mtrp_hub.canonical import stable_id
from mtrp_hub.domain.enums import (
    ReadOperation,
    ReasonCode,
    RepositoryState,
    ResourceType,
    SnapshotKind,
    StepStatus,
    StoreKind,
)
from mtrp_hub.domain.models import (
    Evidence,
    ProjectCoordinate,
    Relation,
    Resource,
    SnapshotRef,
    SourceRef,
    resource_identifier,
)
from mtrp_hub.domain.requests import ReadStep, StepExecution, StepResult
from mtrp_hub.errors import MtrpError
from mtrp_hub.fixtures.schemas import (
    FixtureBundle,
    FixtureRelation,
    MFixtureStatus,
    PFixtureStatus,
    ViewFixtureStatus,
)
from mtrp_hub.fixtures.vault import FixtureMarkdownError, parse_fixture_markdown


class FakeAdapter:
    """Execute exactly one logical Store kind without filesystem or network access."""

    def __init__(self, kind: StoreKind) -> None:
        self.kind = kind

    def execute(
        self,
        step: ReadStep,
        fixture: FixtureBundle,
        context: FakeExecutionContext,
    ) -> StepExecution:
        """Dispatch only on the validated logical step, never on Fixture operations."""
        if step.store is not self.kind:
            raise MtrpError("INTERNAL_ERROR", "FakeAdapter received a mismatched logical Store.", exit_code=1)
        if self.kind is StoreKind.P:
            return self._execute_p(step, fixture, context)
        if self.kind is StoreKind.M:
            return self._execute_m(step, fixture, context)
        return self._execute_view(step, fixture, context)

    @staticmethod
    def _empty(
        step: ReadStep,
        status: StepStatus,
        reason: ReasonCode,
        *,
        coordinate: ProjectCoordinate | None = None,
    ) -> StepExecution:
        return StepExecution(
            step_result=StepResult(
                step_id=step.step_id,
                store=step.store,
                status=status,
                reason_code=reason,
                coordinate=coordinate if coordinate is not None else step.coordinate,
            )
        )

    @staticmethod
    def _relations(
        definitions: Iterable[FixtureRelation],
        resources: tuple[Resource, ...],
    ) -> tuple[Relation, ...]:
        by_key: dict[str, Resource] = {}
        for resource in resources:
            by_key[resource.source_ref.logical_key] = resource
            by_key[resource.content_ref] = resource
        relations: list[Relation] = []
        for definition in definitions:
            try:
                source = by_key[definition.from_key]
                target = by_key[definition.to_key]
            except KeyError as exc:
                raise MtrpError("FIXTURE_INVALID", "Fixture relation has an unknown endpoint.", field="fixture.relations") from exc
            evidence_ids = tuple(sorted(set(source.evidence_ids + target.evidence_ids)))
            relations.append(
                Relation(
                    relation_id=stable_id(
                        "rel-",
                        {
                            "type": definition.relation_type,
                            "from": source.resource_id,
                            "to": target.resource_id,
                            "evidence_ids": evidence_ids,
                        },
                    ),
                    relation_type=definition.relation_type,
                    from_resource_id=source.resource_id,
                    to_resource_id=target.resource_id,
                    evidence_ids=evidence_ids,
                )
            )
        return tuple(relations)

    @staticmethod
    def _execution(
        step: ReadStep,
        resources: tuple[Resource, ...],
        evidence: tuple[Evidence, ...],
        relations: tuple[Relation, ...],
        coordinate: ProjectCoordinate | None = None,
    ) -> StepExecution:
        return StepExecution(
            step_result=StepResult(
                step_id=step.step_id,
                store=step.store,
                status=StepStatus.OK,
                coordinate=coordinate if coordinate is not None else step.coordinate,
                resource_ids=tuple(resource.resource_id for resource in resources),
                evidence_ids=tuple(item.evidence_id for item in evidence),
            ),
            resources=resources,
            relations=relations,
            evidence=evidence,
        )

    def _execute_p(
        self,
        step: ReadStep,
        fixture: FixtureBundle,
        context: FakeExecutionContext,
    ) -> StepExecution:
        data = fixture.p
        if data.status is PFixtureStatus.EMPTY:
            return self._empty(step, StepStatus.EMPTY, ReasonCode.P_EMPTY)
        if data.status is PFixtureStatus.EMPTY_REPOSITORY:
            coordinate = step.coordinate
            if coordinate is not None:
                coordinate = coordinate.model_copy(update={"commit": None, "path": None, "repository_state": RepositoryState.EMPTY})
            return self._empty(step, StepStatus.EMPTY, ReasonCode.EMPTY_REPOSITORY, coordinate=coordinate)
        degraded = {
            PFixtureStatus.BRANCH_MISSING: ReasonCode.BRANCH_NOT_FOUND,
            PFixtureStatus.PERMISSION_DENIED: ReasonCode.PERMISSION_DENIED,
            PFixtureStatus.UNAVAILABLE: ReasonCode.UNAVAILABLE,
            PFixtureStatus.EMPTY_RESPONSE: ReasonCode.EMPTY_RESPONSE,
        }
        if data.status in degraded:
            return self._empty(step, StepStatus.DEGRADED, degraded[data.status])
        if not data.resources:
            return self._empty(step, StepStatus.EMPTY, ReasonCode.P_EMPTY)

        resources: list[Resource] = []
        evidence_items: list[Evidence] = []
        result_coordinate = step.coordinate
        for index, item in enumerate(data.resources):
            if step.operation is ReadOperation.ADR_SEARCH and not any(
                item.path == root or item.path.startswith(f"{root}/") for root in step.adr_roots
            ):
                continue
            if step.coordinate is None:
                raise MtrpError("FIXTURE_INVALID", "P Fixture requires a plan coordinate.", field="fixture.p")
            if (item.project_id, item.repo_id, item.location_id, item.ref) != (
                step.coordinate.project_id,
                step.coordinate.repo_id,
                step.coordinate.location_id,
                step.coordinate.ref,
            ):
                raise MtrpError("FIXTURE_INVALID", "P Fixture attempted to replace the plan coordinate.", field="fixture.p")
            coordinate = step.coordinate.model_copy(
                update={"commit": item.commit, "path": item.path, "repository_state": RepositoryState.READY}
            )
            result_coordinate = coordinate
            snapshot = SnapshotRef(kind=SnapshotKind.GIT_COMMIT, value=item.commit)
            source = SourceRef(
                store=StoreKind.P,
                store_id=step.store_id,
                logical_key=f"{item.repo_id}/{item.path}",
            )
            evidence_id = stable_id(
                "evd-",
                {
                    "source": source,
                    "snapshot": snapshot,
                    "content_sha256": item.content_sha256,
                    "step": step.step_id,
                    "plan_hash": context.plan_hash,
                    "path": item.path,
                },
            )
            evidence = Evidence(
                evidence_id=evidence_id,
                source_ref=source,
                snapshot_ref=snapshot,
                project_coordinate=coordinate,
                path=item.path,
                entry_id=None,
                content_sha256=item.content_sha256,
                request_hash=context.request_hash,
                plan_hash=context.plan_hash,
                step_id=step.step_id,
            )
            resource = Resource(
                resource_id=resource_identifier(source, snapshot, item.content_sha256),
                resource_type=ResourceType.ADR if step.operation is ReadOperation.ADR_SEARCH else ResourceType.PROJECT_FILE,
                title=item.title,
                content_ref=f"fixture:{fixture.fixture_id}:p:{index}",
                source_ref=source,
                snapshot_ref=snapshot,
                project_coordinate=coordinate,
                evidence_ids=(evidence_id,),
            )
            resources.append(resource)
            evidence_items.append(evidence)
        if not resources:
            reason = ReasonCode.ADR_MISSING if step.operation is ReadOperation.ADR_SEARCH else ReasonCode.P_EMPTY
            return self._empty(step, StepStatus.EMPTY, reason)
        resource_tuple = tuple(resources)
        relations = self._relations(data.relations, resource_tuple)
        return self._execution(step, resource_tuple, tuple(evidence_items), relations, result_coordinate)

    def _execute_m(
        self,
        step: ReadStep,
        fixture: FixtureBundle,
        context: FakeExecutionContext,
    ) -> StepExecution:
        data = fixture.m
        if not data.space_resolved:
            return self._empty(step, StepStatus.DEGRADED, ReasonCode.UNAVAILABLE)
        if data.status is MFixtureStatus.EMPTY:
            return self._empty(step, StepStatus.EMPTY, ReasonCode.M_EMPTY)
        degraded = {
            MFixtureStatus.PERMISSION_DENIED: ReasonCode.PERMISSION_DENIED,
            MFixtureStatus.UNAVAILABLE: ReasonCode.UNAVAILABLE,
            MFixtureStatus.EMPTY_RESPONSE: ReasonCode.EMPTY_RESPONSE,
        }
        if data.status in degraded:
            return self._empty(step, StepStatus.DEGRADED, degraded[data.status])
        if not data.resources:
            return self._empty(step, StepStatus.EMPTY, ReasonCode.M_EMPTY)
        resources: list[Resource] = []
        evidence_items: list[Evidence] = []
        for index, item in enumerate(data.resources):
            snapshot = SnapshotRef(kind=SnapshotKind.CONTENT_SHA256, value=item.content_sha256)
            source = SourceRef(store=StoreKind.M, store_id=step.store_id, logical_key=f"entry:{item.entry_id}")
            evidence_id = stable_id(
                "evd-",
                {
                    "source": source,
                    "snapshot": snapshot,
                    "version": item.version,
                    "step": step.step_id,
                    "plan_hash": context.plan_hash,
                },
            )
            evidence = Evidence(
                evidence_id=evidence_id,
                source_ref=source,
                snapshot_ref=snapshot,
                entry_id=item.entry_id,
                content_sha256=item.content_sha256,
                request_hash=context.request_hash,
                plan_hash=context.plan_hash,
                step_id=step.step_id,
            )
            resources.append(
                Resource(
                    resource_id=resource_identifier(source, snapshot, item.content_sha256),
                    resource_type=ResourceType.KNOWLEDGE_ENTRY,
                    title=item.title,
                    content_ref=f"fixture:{fixture.fixture_id}:m:{index}",
                    source_ref=source,
                    snapshot_ref=snapshot,
                    evidence_ids=(evidence_id,),
                )
            )
            evidence_items.append(evidence)
        resource_tuple = tuple(resources)
        relations = self._relations(data.relations, resource_tuple)
        return self._execution(step, resource_tuple, tuple(evidence_items), relations)

    def _execute_view(
        self,
        step: ReadStep,
        fixture: FixtureBundle,
        context: FakeExecutionContext,
    ) -> StepExecution:
        data = fixture.t if step.store is StoreKind.T else fixture.r
        empty_reason = ReasonCode.T_EMPTY if step.store is StoreKind.T else ReasonCode.R_EMPTY
        if data.status is ViewFixtureStatus.EMPTY:
            return self._empty(step, StepStatus.EMPTY, empty_reason)
        degraded = {
            ViewFixtureStatus.PERMISSION_DENIED: ReasonCode.PERMISSION_DENIED,
            ViewFixtureStatus.UNAVAILABLE: ReasonCode.UNAVAILABLE,
            ViewFixtureStatus.EMPTY_RESPONSE: ReasonCode.EMPTY_RESPONSE,
        }
        if data.status in degraded:
            return self._empty(step, StepStatus.DEGRADED, degraded[data.status])
        resources: list[Resource] = []
        evidence_items: list[Evidence] = []
        required_tag = "mtr/trial" if step.store is StoreKind.T else "mtr/rarely"
        try:
            parsed_documents = [parse_fixture_markdown(document.raw_markdown, logical_path=document.path) for document in data.documents]
        except FixtureMarkdownError:
            return self._empty(step, StepStatus.DEGRADED, ReasonCode.INVALID_YAML)
        for index, (document, parsed) in enumerate(zip(data.documents, parsed_documents, strict=True)):
            if parsed.needs_classification or required_tag not in parsed.tags:
                continue
            snapshot = SnapshotRef(kind=SnapshotKind.CONTENT_SHA256, value=parsed.content_sha256)
            source = SourceRef(store=step.store, store_id=step.store_id, logical_key=f"note:{document.path}")
            evidence_id = stable_id(
                "evd-",
                {
                    "source": source,
                    "snapshot": snapshot,
                    "step": step.step_id,
                    "plan_hash": context.plan_hash,
                    "path": document.path,
                },
            )
            evidence = Evidence(
                evidence_id=evidence_id,
                source_ref=source,
                snapshot_ref=snapshot,
                path=document.path,
                content_sha256=parsed.content_sha256,
                request_hash=context.request_hash,
                plan_hash=context.plan_hash,
                step_id=step.step_id,
            )
            resources.append(
                Resource(
                    resource_id=resource_identifier(source, snapshot, parsed.content_sha256),
                    resource_type=ResourceType.VAULT_NOTE,
                    title=parsed.title,
                    content_ref=f"fixture:{fixture.fixture_id}:{step.store.value.lower()}:{index}",
                    source_ref=source,
                    snapshot_ref=snapshot,
                    evidence_ids=(evidence_id,),
                )
            )
            evidence_items.append(evidence)
        if not resources:
            return self._empty(step, StepStatus.EMPTY, empty_reason)
        resource_tuple = tuple(resources)
        relations = self._relations(data.relations, resource_tuple)
        return self._execution(step, resource_tuple, tuple(evidence_items), relations)
