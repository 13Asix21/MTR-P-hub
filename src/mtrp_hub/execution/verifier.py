"""Intrinsic and expected-recompile Manifest verification before every execution."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from mtrp_hub.canonical import sha256_json
from mtrp_hub.errors import MtrpError
from mtrp_hub.execution.binding import BindingResolver
from mtrp_hub.execution.models import ExecutionManifest, ManifestVerification
from mtrp_hub.execution.profile import STAGE2_PROFILE, Stage2ReadonlyProfile


class ManifestVerifier:
    def __init__(self, profile: Stage2ReadonlyProfile | None = None, binding_resolver: BindingResolver | None = None) -> None:
        self._profile = profile or STAGE2_PROFILE
        self._binding_resolver = binding_resolver or BindingResolver(self._profile)

    def verify_intrinsic(self, manifest: ExecutionManifest) -> None:
        try:
            ExecutionManifest.model_validate(manifest.model_dump(mode="json"))
        except ValidationError as exc:
            raise MtrpError("MANIFEST_INVALID", "Manifest schema validation failed.", field="manifest") from exc
        expected_hash = sha256_json(manifest.hashed_payload())
        if manifest.manifest_hash != expected_hash:
            raise MtrpError("MANIFEST_HASH_MISMATCH", "Manifest hash verification failed.", field="manifest_hash")
        if manifest.execution_id != f"exe_{expected_hash}":
            raise MtrpError("MANIFEST_IDENTITY_MISMATCH", "Execution identity is not derived from the Manifest hash.", field="execution_id")
        if manifest.request_id != f"req_{manifest.request_hash}" or manifest.route_id != f"route_{manifest.plan_hash}":
            raise MtrpError("MANIFEST_IDENTITY_MISMATCH", "Request or route identity is invalid.", field="request_id")
        if manifest.profile_hash != self._profile.profile_hash():
            raise MtrpError("TOOL_PROFILE_UNSUPPORTED", "The tool profile fingerprint is unsupported.", field="profile_hash")
        if len(manifest.steps) > manifest.limits.max_steps:
            raise MtrpError("MANIFEST_STEP_LIMIT_EXCEEDED", "Manifest step count exceeds its limits.", field="steps")
        self._binding_resolver.validate_manifest(manifest)

    def verify_expected(self, actual: ExecutionManifest, expected: ExecutionManifest) -> ManifestVerification:
        self.verify_intrinsic(actual)
        self.verify_intrinsic(expected)
        actual_data = actual.model_dump(mode="json")
        expected_data = expected.model_dump(mode="json")
        self._compare(actual_data, expected_data, "manifest")
        return ManifestVerification(
            request_id=actual.request_id,
            route_id=actual.route_id,
            execution_id=actual.execution_id,
            manifest_hash=actual.manifest_hash,
        )

    def verify_before_call(self, manifest: ExecutionManifest, expected_hash: str) -> None:
        self.verify_intrinsic(manifest)
        if manifest.manifest_hash != expected_hash:
            raise MtrpError("MANIFEST_CONTEXT_CHANGED", "The Manifest differs from the expected recompiled hash.", field="manifest_hash")

    def _compare(self, actual: Any, expected: Any, path: str) -> None:
        if type(actual) is not type(expected):
            self._mismatch(path)
        if isinstance(actual, dict):
            if set(actual) != set(expected):
                self._mismatch(path)
            for key in sorted(actual):
                self._compare(actual[key], expected[key], f"{path}.{key}")
            return
        if isinstance(actual, list):
            if len(actual) != len(expected):
                self._mismatch(path)
            for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
                self._compare(left, right, f"{path}.{index}")
            return
        if actual != expected:
            self._mismatch(path)

    @staticmethod
    def _mismatch(path: str) -> None:
        if path.endswith("manifest_hash"):
            raise MtrpError("MANIFEST_HASH_MISMATCH", "The supplied Manifest hash differs from the expected Manifest.", field="manifest_hash")
        if path.endswith(("execution_id", "request_id", "route_id", "request_hash", "plan_hash")):
            raise MtrpError("MANIFEST_IDENTITY_MISMATCH", "Manifest execution identity differs from the expected plan.", field=path)
        raise MtrpError("MANIFEST_CONTEXT_CHANGED", "Manifest content differs from the request and trusted configuration.", field=path)


__all__ = ["ManifestVerifier"]
