"""The only stage 0+1 adapter protocol and its in-memory execution context."""

from __future__ import annotations

from typing import Protocol

from mtrp_hub.domain.models import FrozenModel, Sha256
from mtrp_hub.domain.requests import ReadStep, StepExecution
from mtrp_hub.fixtures.schemas import FixtureBundle
from mtrp_hub.security import AccessDecision


class FakeExecutionContext(FrozenModel):
    """Internal trace context; deliberately excludes tools, URLs, and target paths."""

    request_hash: Sha256
    plan_hash: Sha256
    fixture_hash: Sha256
    access_decision: AccessDecision


class ReadOnlyAdapter(Protocol):
    """Execute one logical read step against validated in-memory Fixtures."""

    def execute(
        self,
        step: ReadStep,
        fixture: FixtureBundle,
        context: FakeExecutionContext,
    ) -> StepExecution:
        """Return status and Canonical objects together, without external I/O."""
        raise TypeError("ReadOnlyAdapter is a protocol; use a concrete FakeAdapter")
