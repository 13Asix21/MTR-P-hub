"""Strict synthetic stage 2 Fixture contracts; never labeled as real evidence."""

from __future__ import annotations

from pydantic import Field, model_validator

from mtrp_hub.domain.models import FrozenModel, SafeId
from mtrp_hub.execution.envelope import RawResponse, RawToolOutcome
from mtrp_hub.execution.models import Stage2Operation


class Stage2Fixture(FrozenModel):
    schema_version: str = Field(pattern=r"^1$", default="1")
    fixture_id: SafeId
    responses: dict[Stage2Operation, tuple[RawToolOutcome, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_queues(self) -> "Stage2Fixture":
        for outcomes in self.responses.values():
            for outcome in outcomes:
                if outcome.response is None and outcome.error is None:
                    raise ValueError("Fixture outcomes must explicitly declare response or error")
        return self


__all__ = ["RawResponse", "RawToolOutcome", "Stage2Fixture"]
