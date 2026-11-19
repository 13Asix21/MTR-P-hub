"""Shared stage 2 adapter context; contains no network or SDK port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mtrp_hub.config.stage2 import LoadedStage2Config
from mtrp_hub.domain.requests import ReadStep
from mtrp_hub.domain.requests_v2 import StepExecutionV2
from mtrp_hub.execution.models import ExecutionManifest, ExecutionMode
from mtrp_hub.execution.profile import STAGE2_PROFILE, Stage2ReadonlyProfile


@dataclass(frozen=True)
class Stage2ExecutionContext:
    config: LoadedStage2Config
    manifest: ExecutionManifest
    route_step: ReadStep
    profile: Stage2ReadonlyProfile = STAGE2_PROFILE

    @property
    def mode(self) -> ExecutionMode:
        return self.manifest.mode

    @property
    def trace_id(self) -> str:
        return self.manifest.trace_id()


class Stage2Normalizer(Protocol):
    def normalize(self, route_step: ReadStep, receipts: tuple[object, ...], context: Stage2ExecutionContext) -> StepExecutionV2:
        """Normalize controlled receipts without performing external I/O."""
        raise TypeError("Stage2Normalizer is a protocol")


__all__ = ["Stage2ExecutionContext", "Stage2Normalizer"]
