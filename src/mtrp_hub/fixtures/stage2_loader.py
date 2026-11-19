"""Cwd-confined, size-limited strict JSON loading for stage 2 handoff and Fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from mtrp_hub.errors import MtrpError, safe_validation_field
from mtrp_hub.execution.envelope import ExecutionEnvelope
from mtrp_hub.execution.models import ExecutionManifest
from mtrp_hub.fixtures.stage2_schemas import Stage2Fixture

ModelT = TypeVar("ModelT", bound=BaseModel)


class Stage2FixtureLoader:
    def load(self, path: str, *, max_bytes: int = 16 * 1024 * 1024) -> Stage2Fixture:
        return self.load_model(path, model=Stage2Fixture, field="fixture", max_bytes=max_bytes)

    def load_model(
        self,
        path: str,
        *,
        model: type[ModelT],
        field: str,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> ModelT:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts or candidate.suffix.lower() != ".json" or "\\" in path:
            raise MtrpError("PATH_ESCAPE", "Stage 2 Fixtures must be cwd-relative JSON paths.", field=field)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(Path.cwd().resolve())
            with resolved.open("rb") as stream:
                raw = stream.read(max_bytes + 1)
        except (OSError, ValueError) as exc:
            raise MtrpError("FIXTURE_NOT_FOUND", "The stage 2 Fixture cannot be read.", field=field) from exc
        if len(raw) > max_bytes:
            raise MtrpError("ENVELOPE_TOO_LARGE", "The Fixture exceeds its input size limit.", field=field)
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise MtrpError("FIXTURE_PARSE_ERROR", "The stage 2 Fixture is not UTF-8.", field=field) from exc
        return self.loads(text, model=model, field=field)

    @staticmethod
    def _parse(text: str, *, field: str) -> Any:
        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        def invalid_constant(_: str) -> None:
            raise ValueError("non-finite number")

        try:
            return json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            code = "ENVELOPE_INVALID" if field == "envelope" else "MANIFEST_INVALID" if field == "manifest" else "FIXTURE_PARSE_ERROR"
            raise MtrpError(code, "Controlled JSON input is invalid.", field=field) from exc

    def loads(self, text: str, *, model: type[ModelT], field: str) -> ModelT:
        data = self._parse(text, field=field)
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            safe_field = safe_validation_field(field, exc.errors(include_url=False, include_input=False))
            code = "ENVELOPE_INVALID" if field == "envelope" else "MANIFEST_INVALID" if field == "manifest" else "FIXTURE_INVALID"
            raise MtrpError(code, "Controlled JSON schema validation failed.", field=safe_field) from exc

    def load_manifest(self, text: str) -> ExecutionManifest:
        return self.loads(text, model=ExecutionManifest, field="manifest")

    def load_envelope(self, text: str) -> ExecutionEnvelope:
        return self.loads(text, model=ExecutionEnvelope, field="envelope")


__all__ = ["Stage2FixtureLoader"]
