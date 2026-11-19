"""Offline Fixture/Spy transport; no sockets, subprocesses, SDKs, or Git calls."""

from __future__ import annotations

from dataclasses import dataclass

from mtrp_hub.config.stage2 import LoadedStage2Config
from mtrp_hub.domain.models import FrozenModel
from mtrp_hub.execution.envelope import RawResponse, RawToolOutcome, ToolErrorKind
from mtrp_hub.execution.models import Stage2Operation
from mtrp_hub.execution.profile import STAGE2_PROFILE, Stage2ReadonlyProfile
from mtrp_hub.fixtures.stage2_schemas import Stage2Fixture


class ToolInvocation(FrozenModel):
    tool_id: str
    inner_tool_name: str | None
    arguments: dict
    timeout_seconds: int


@dataclass
class Stage2FixtureTransport:
    """Return explicit synthetic responses in logical operation order."""

    config: LoadedStage2Config
    fixture: Stage2Fixture | None = None
    profile: Stage2ReadonlyProfile = STAGE2_PROFILE

    def __post_init__(self) -> None:
        self._queues = {operation: list(outcomes) for operation, outcomes in (self.fixture.responses if self.fixture else {}).items()}
        self._calls: list[ToolInvocation] = []

    @property
    def calls(self) -> tuple[ToolInvocation, ...]:
        return tuple(self._calls)

    def invoke(self, tool_id: str, inner_tool_name: str | None, arguments: dict, timeout_seconds: int) -> RawToolOutcome:
        operation = self.profile.operation_for_call(tool_id, inner_tool_name, arguments)
        self._calls.append(
            ToolInvocation(
                tool_id=tool_id,
                inner_tool_name=inner_tool_name,
                arguments=arguments,
                timeout_seconds=timeout_seconds,
            )
        )
        queue = self._queues.get(operation, [])
        if queue:
            return queue.pop(0)
        return self._default(operation)

    def _default(self, operation: Stage2Operation) -> RawToolOutcome:
        if operation is Stage2Operation.LEXIANG_RESOLVE_SPACE:
            config = self.config.lexiang_m
            return RawToolOutcome(
                response=RawResponse(
                    kind="json",
                    data={
                        "code": 0,
                        "message": "success",
                        "data": {
                            "space": {"id": config.space_id, "team_id": config.team_id, "root_entry_id": config.root_entry_id},
                            "config": {},
                        },
                        "request_id": "synthetic-fixture",
                    },
                )
            )
        if operation in (Stage2Operation.LEXIANG_SEARCH_TEXT, Stage2Operation.LEXIANG_SEARCH_VECTOR):
            return RawToolOutcome(response=RawResponse(kind="json", data={"code": 0, "message": "success", "data": {}, "request_id": "synthetic-fixture"}))
        if operation is Stage2Operation.GITHUB_LIST_BRANCHES:
            return RawToolOutcome(response=RawResponse(kind="json", data=[]))
        if operation is Stage2Operation.GITHUB_READ_REPOSITORY_ROOT:
            return RawToolOutcome(response=RawResponse(kind="json", data=[]))
        return RawToolOutcome(error=ToolErrorKind.NOT_FOUND)


__all__ = ["Stage2FixtureTransport", "ToolInvocation"]
