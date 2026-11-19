"""Explicit cwd-confined JSON Fixture loading and plan-coordinate validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from mtrp_hub.domain.enums import StoreKind
from mtrp_hub.domain.requests import ReadPlan
from mtrp_hub.errors import MtrpError, safe_validation_field
from mtrp_hub.fixtures.schemas import FixtureBundle


class FixtureLoader:
    """Load one explicit relative JSON file; no directory scanning or discovery."""

    def load(self, path: str | None) -> FixtureBundle:
        """Load a Fixture only after all request and Registry guards have passed."""
        if path is None:
            return FixtureBundle.empty()
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts or candidate.suffix.lower() != ".json":
            raise MtrpError("PATH_ESCAPE", "Fixture must remain a relative JSON path.", field="fixture")
        try:
            cwd = Path.cwd().resolve()
            resolved = candidate.resolve()
            resolved.relative_to(cwd)
        except (OSError, ValueError) as exc:
            raise MtrpError("PATH_ESCAPE", "Fixture escapes the current working directory.", field="fixture") from exc
        try:
            text = resolved.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise MtrpError("FIXTURE_NOT_FOUND", "Fixture JSON was not found.", field="fixture") from exc
        except (OSError, UnicodeError) as exc:
            raise MtrpError("FIXTURE_NOT_FOUND", "Fixture JSON cannot be read.", field="fixture") from exc
        return self.loads(text)

    @staticmethod
    def loads(text: str) -> FixtureBundle:
        """Parse JSON with duplicate-key and non-finite-number rejection."""
        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        def invalid_constant(_: str) -> None:
            raise ValueError("non-finite JSON number")

        try:
            data = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise MtrpError("FIXTURE_PARSE_ERROR", "Fixture JSON is invalid.", field="fixture") from exc
        try:
            return FixtureBundle.model_validate(data)
        except ValidationError as exc:
            field = safe_validation_field(
                "fixture",
                exc.errors(include_url=False, include_input=False),
            )
            raise MtrpError("FIXTURE_INVALID", "Fixture schema validation failed.", field=field) from exc


def validate_fixture_for_plan(bundle: FixtureBundle, plan: ReadPlan) -> None:
    """Ensure Fixture P data cannot replace the Registry-resolved coordinate."""
    p_steps = [step for step in plan.steps if step.store is StoreKind.P]
    if not p_steps:
        return
    coordinate = p_steps[0].coordinate
    if coordinate is None:
        raise MtrpError("FIXTURE_INVALID", "P Fixture lacks a resolved plan coordinate.", field="fixture.p")
    expected = (coordinate.project_id, coordinate.repo_id, coordinate.location_id, coordinate.ref)
    repository = bundle.p.repository
    if repository is not None:
        actual = (repository.project_id, repository.repo_id, repository.location_id, repository.ref)
        if actual != expected:
            raise MtrpError("FIXTURE_INVALID", "Fixture cannot replace the authoritative P coordinate.", field="fixture.p.repository")
        if bundle.p.resources and repository.commit != bundle.p.resources[0].commit:
            raise MtrpError("FIXTURE_INVALID", "Fixture P snapshot is inconsistent.", field="fixture.p.repository")
    for resource in bundle.p.resources:
        actual = (resource.project_id, resource.repo_id, resource.location_id, resource.ref)
        if actual != expected:
            raise MtrpError("FIXTURE_INVALID", "Fixture P resource does not match the plan coordinate.", field="fixture.p.resources")
