"""Read-only Obsidian adapter with lexical, filesystem, and double-read guards."""

from __future__ import annotations

import os
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterator

from mtrp_hub.adapters.obsidian_markdown import ObsidianMarkdownError, ObsidianMarkdownParser
from mtrp_hub.adapters.stage2_base import Stage2ExecutionContext
from mtrp_hub.canonical import sha256_bytes, sha256_json
from mtrp_hub.config.stage2 import ManagedRootConfig, ObsidianStage2Config
from mtrp_hub.domain.enums import ResourceType, SnapshotKind, StoreKind
from mtrp_hub.domain.models import FrozenModel, SnapshotRef, SourceRef, validate_relative_path
from mtrp_hub.domain.models_v2 import EvidenceV2, ResourceV2
from mtrp_hub.domain.requests_v2 import OperationResult, StepExecutionV2, StepResultV2, StepStatusV2
from mtrp_hub.errors import MtrpError
from mtrp_hub.execution import identity
from mtrp_hub.execution.models import ManifestStep, Stage2Operation

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_REJECTED_VAULT_CODES = {"PATH_ESCAPE", "SYMLINK_ESCAPE", "VAULT_PATH_COLLISION"}


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "FileIdentity":
        return cls(int(value.st_dev), int(value.st_ino), int(value.st_size), int(value.st_mtime_ns))


@dataclass(frozen=True)
class PreparedVaultScope:
    config: ObsidianStage2Config
    resolved_root: Path
    managed_roots: tuple[tuple[ManagedRootConfig, Path], ...]


@dataclass(frozen=True)
class VaultCandidate:
    actual_path: Path
    relative_path: str
    identity: FileIdentity


@dataclass(frozen=True)
class VerifiedVaultFile:
    candidate: VaultCandidate
    raw_bytes: bytes
    content_sha256: str


@dataclass
class VaultCounters:
    scanned_count: int = 0
    matched_count: int = 0
    unmanaged_count: int = 0
    conflict_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    excluded_count: int = 0
    symlink_count: int = 0
    content_hashes: list[tuple[str, str]] = field(default_factory=list)


class VaultInventory(FrozenModel):
    scanned_count: int = 0
    matched_count: int = 0
    unmanaged_count: int = 0
    conflict_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    excluded_count: int = 0
    symlink_count: int = 0
    aggregate_sha256: str


class SafeVaultIO:
    """The only target file I/O boundary used by the real Obsidian adapter."""

    def prepare_scope(self, config: ObsidianStage2Config) -> PreparedVaultScope:
        """Resolve only the explicit trust anchor and configured managed roots."""
        try:
            root_anchor = Path(config.vault_root)
            os.lstat(root_anchor)
            resolved_root = root_anchor.resolve(strict=True)
            if not resolved_root.is_dir():
                raise OSError("not a directory")
        except (OSError, ValueError) as exc:
            raise MtrpError("VAULT_ROOT_UNAVAILABLE", "The configured Vault root is unavailable.", field="vault_root") from exc
        prepared: list[tuple[ManagedRootConfig, Path]] = []
        for managed in config.managed_roots:
            candidate = root_anchor.joinpath(*PurePosixPath(managed.path).parts)
            try:
                self.check_no_reparse(candidate, managed_root=True)
                resolved = candidate.resolve(strict=True)
                self.check_containment(resolved_root, resolved)
                if not resolved.is_dir():
                    raise OSError("managed root is not a directory")
            except MtrpError:
                raise
            except (OSError, ValueError) as exc:
                raise MtrpError("VAULT_ROOT_UNAVAILABLE", "A configured managed root is unavailable.", field="managed_roots") from exc
            prepared.append((managed, resolved))
        return PreparedVaultScope(config=config, resolved_root=resolved_root, managed_roots=tuple(prepared))

    def iter_markdown(self, scope: PreparedVaultScope, counters: VaultCounters | None = None) -> Iterator[VaultCandidate]:
        """Traverse only managed roots with scandir; never discover or rglob the Vault."""
        counts = counters or VaultCounters()
        seen_logical: dict[str, Path] = {}
        for managed, resolved_managed in scope.managed_roots:
            stack = [resolved_managed]
            while stack:
                directory = stack.pop()
                try:
                    self.check_no_reparse(directory)
                    self.check_containment(scope.resolved_root, directory.resolve(strict=True))
                    with os.scandir(directory) as entries:
                        ordered_entries = sorted(entries, key=lambda item: item.name)
                except MtrpError as exc:
                    if exc.error_code in _REJECTED_VAULT_CODES:
                        raise
                    counts.error_count += 1
                    counts.skipped_count += 1
                    continue
                except OSError:
                    counts.error_count += 1
                    counts.skipped_count += 1
                    continue
                for entry in ordered_entries:
                    actual = Path(entry.path)
                    try:
                        relative_actual = actual.relative_to(scope.resolved_root)
                        logical = unicodedata.normalize("NFC", PurePosixPath(*relative_actual.parts).as_posix())
                        if any(0xD800 <= ord(char) <= 0xDFFF for char in logical):
                            raise ValueError("non-UTF-8 filesystem name")
                        validate_relative_path(logical)
                    except (ValueError, OSError):
                        counts.error_count += 1
                        counts.skipped_count += 1
                        continue
                    if self._is_excluded(scope.config, logical):
                        counts.excluded_count += 1
                        continue
                    try:
                        metadata = self.check_no_reparse(actual)
                    except MtrpError:
                        counts.symlink_count += 1
                        counts.skipped_count += 1
                        continue
                    if stat.S_ISDIR(metadata.st_mode):
                        try:
                            resolved_directory = actual.resolve(strict=True)
                            self.check_containment(scope.resolved_root, resolved_directory)
                        except (OSError, ValueError, MtrpError):
                            counts.error_count += 1
                            counts.skipped_count += 1
                            continue
                        stack.append(resolved_directory)
                        continue
                    if not stat.S_ISREG(metadata.st_mode) or not logical.casefold().endswith(".md"):
                        counts.skipped_count += 1
                        continue
                    if not self._lexical_allow(scope.config, logical):
                        counts.skipped_count += 1
                        continue
                    counts.scanned_count += 1
                    if counts.scanned_count > scope.config.max_scan_files:
                        raise MtrpError("VAULT_SCAN_LIMIT_EXCEEDED", "The Vault scan limit was exceeded.", field="max_scan_files")
                    previous = seen_logical.get(logical)
                    if previous is not None and os.path.normcase(os.fspath(previous)) != os.path.normcase(os.fspath(actual)):
                        raise MtrpError("VAULT_PATH_COLLISION", "Two Vault paths collide after NFC normalization.", field="managed_roots")
                    seen_logical[logical] = actual
                    try:
                        self.check_containment(scope.resolved_root, actual.parent.resolve(strict=True))
                    except (OSError, ValueError, MtrpError) as exc:
                        raise MtrpError("PATH_ESCAPE", "A Vault candidate escapes its resolved parent scope.", field="managed_roots") from exc
                    yield VaultCandidate(actual, logical, FileIdentity.from_stat(metadata))

    def open_verified(self, candidate: VaultCandidate, scope: PreparedVaultScope) -> VerifiedVaultFile:
        """Open rb only after all checks, then verify identity and hash twice."""
        maximum = scope.config.max_file_bytes
        try:
            before = os.lstat(candidate.actual_path)
            self.check_no_reparse(candidate.actual_path)
            if not stat.S_ISREG(before.st_mode):
                raise MtrpError("VAULT_FILE_CHANGED", "A Vault file is no longer regular.", field="managed_roots")
            before_identity = FileIdentity.from_stat(before)
            if before_identity.inode == 0:
                raise MtrpError("VAULT_FILE_CHANGED", "Vault file identity is insufficient on this platform.", field="managed_roots")
            if before_identity != candidate.identity:
                raise MtrpError("VAULT_FILE_CHANGED", "A Vault file changed after scanning.", field="managed_roots")
            if before.st_size > maximum:
                raise MtrpError("VAULT_FILE_TOO_LARGE", "A Vault file exceeds the configured size limit.", field="max_file_bytes")
            self.check_containment(scope.resolved_root, candidate.actual_path.parent.resolve(strict=True))
            with self._open_binary_no_follow(candidate.actual_path) as handle:
                opened = os.fstat(handle.fileno())
                opened_identity = FileIdentity.from_stat(opened)
                if not stat.S_ISREG(opened.st_mode) or opened_identity != before_identity:
                    raise MtrpError("VAULT_FILE_CHANGED", "Vault file identity changed while opening.", field="managed_roots")
                first = handle.read(maximum + 1)
                if len(first) > maximum:
                    raise MtrpError("VAULT_FILE_TOO_LARGE", "A Vault file grew beyond the size limit.", field="max_file_bytes")
                first_hash = sha256_bytes(first)
                handle.seek(0)
                second = handle.read(maximum + 1)
                second_hash = sha256_bytes(second)
                after = os.fstat(handle.fileno())
                if first != second or first_hash != second_hash or FileIdentity.from_stat(after) != opened_identity:
                    raise MtrpError("VAULT_FILE_CHANGED", "Vault bytes or identity changed during reading.", field="managed_roots")
            final = os.lstat(candidate.actual_path)
            self.check_no_reparse(candidate.actual_path)
            if FileIdentity.from_stat(final) != before_identity:
                raise MtrpError("VAULT_FILE_CHANGED", "Vault file identity changed after reading.", field="managed_roots")
            self.check_containment(scope.resolved_root, candidate.actual_path.parent.resolve(strict=True))
            return VerifiedVaultFile(candidate=candidate, raw_bytes=first, content_sha256=first_hash)
        except MtrpError:
            raise
        except (OSError, ValueError) as exc:
            raise MtrpError("VAULT_READ_FAILED", "A Vault file could not be read safely.", field="managed_roots") from exc

    @staticmethod
    def _open_binary_no_follow(path: Path):  # type: ignore[no-untyped-def]
        """Use O_NOFOLLOW where available while retaining a binary file handle."""
        if hasattr(os, "O_NOFOLLOW"):
            def opener(file: str, flags: int) -> int:
                return os.open(file, flags | os.O_NOFOLLOW)

            return open(path, "rb", opener=opener)
        return open(path, "rb")

    @staticmethod
    def check_no_reparse(path: Path, *, managed_root: bool = False) -> os.stat_result:
        """Use lstat plus Windows reparse attributes; never resolve a link target."""
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise MtrpError("VAULT_READ_FAILED", "A Vault path cannot be inspected.", field="managed_roots") from exc
        is_reparse = bool(getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)
        if path.is_symlink() or stat.S_ISLNK(metadata.st_mode) or is_reparse:
            code = "SYMLINK_ESCAPE" if managed_root else "SYMLINK_ESCAPE"
            raise MtrpError(code, "Symbolic links and reparse points are not followed.", field="managed_roots")
        return metadata

    @staticmethod
    def check_containment(root: Path, candidate: Path) -> None:
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise MtrpError("PATH_ESCAPE", "A Vault path escapes the authorized root.", field="managed_roots") from exc

    @staticmethod
    def _is_excluded(config: ObsidianStage2Config, relative_path: str) -> bool:
        return any(relative_path == root or relative_path.startswith(f"{root}/") for root in config.excluded_roots)

    def _lexical_allow(self, config: ObsidianStage2Config, relative_path: str) -> bool:
        if self._is_excluded(config, relative_path):
            return False
        return any(
            relative_path == managed.path or relative_path.startswith(f"{managed.path}/")
            for managed in config.managed_roots
        ) and relative_path.casefold().endswith(".md")


class ObsidianAdapter:
    """Execute a fixed tag view over verified raw Markdown bytes."""

    def __init__(self, parser: ObsidianMarkdownParser | None = None, io: SafeVaultIO | None = None) -> None:
        self._parser = parser or ObsidianMarkdownParser()
        self._io = io or SafeVaultIO()

    def execute(self, step: ManifestStep, context: Stage2ExecutionContext) -> StepExecutionV2:
        if step.operation is not Stage2Operation.VAULT_VIEW_SEARCH:
            raise MtrpError("ARGUMENT_FORBIDDEN", "ObsidianAdapter only accepts fixed Vault views.", field=step.step_id)
        expected_scope = context.config.obsidian.scope_sha256
        if step.arguments.get("scope_sha256") != expected_scope or step.arguments.get("vault_id") != context.config.obsidian.vault_id:
            raise MtrpError("CORE_STATE_CHANGED", "The Vault scope differs from trusted configuration.", field=step.step_id)
        view_tag = step.arguments.get("view_tag")
        expected_tag = context.config.obsidian.trial_tag if context.route_step.store is StoreKind.T else context.config.obsidian.rarely_tag
        if view_tag != expected_tag or view_tag != context.route_step.view_tag:
            raise MtrpError("ARGUMENT_FORBIDDEN", "The Vault view tag is not authorized for this route step.", field=step.step_id)
        counters = VaultCounters()
        try:
            scope = self._io.prepare_scope(context.config.obsidian)
            candidates = tuple(self._io.iter_markdown(scope, counters))
        except MtrpError as exc:
            if exc.error_code in _REJECTED_VAULT_CODES or exc.error_code == "VAULT_ROOT_UNAVAILABLE":
                raise
            return self._empty_or_degraded(step, context, counters, exc.error_code)
        resources: list[ResourceV2] = []
        evidence_items: list[EvidenceV2] = []
        for candidate in sorted(candidates, key=lambda item: item.relative_path):
            try:
                verified = self._io.open_verified(candidate, scope)
                parsed = self._parser.parse(verified.raw_bytes, candidate.relative_path)
                counters.content_hashes.append((candidate.relative_path, verified.content_sha256))
            except ObsidianMarkdownError:
                counters.error_count += 1
                counters.skipped_count += 1
                continue
            except MtrpError as exc:
                if exc.error_code in _REJECTED_VAULT_CODES:
                    raise
                counters.error_count += 1
                counters.skipped_count += 1
                continue
            if parsed.classification == "conflict":
                counters.conflict_count += 1
                counters.skipped_count += 1
                continue
            if parsed.classification == "unmanaged":
                counters.unmanaged_count += 1
                counters.skipped_count += 1
                continue
            wanted = "trial" if view_tag == context.config.obsidian.trial_tag else "rarely"
            if parsed.classification != wanted:
                counters.skipped_count += 1
                continue
            try:
                resource, evidence = self._canonical(step, context, candidate.relative_path, parsed.title, verified.content_sha256)
            except ValueError:
                # A valid filesystem name/title may still be unrepresentable in
                # the legacy non-fetchable SourceRef/Canonical value objects.
                counters.error_count += 1
                counters.skipped_count += 1
                continue
            resources.append(resource)
            evidence_items.append(evidence)
            counters.matched_count += 1
        if len(resources) > context.config.obsidian.max_results:
            return self._empty_or_degraded(step, context, counters, "VAULT_SCAN_LIMIT_EXCEEDED")
        status = StepStatusV2.DEGRADED if counters.error_count else StepStatusV2.OK if resources else StepStatusV2.EMPTY
        reason = "VAULT_PARTIAL_FAILURE" if counters.error_count else None
        if not resources and not counters.error_count:
            reason = "T_EMPTY" if context.route_step.store is StoreKind.T else "R_EMPTY"
        operation = OperationResult(
            step_id=step.step_id,
            status=status,
            reason_code=reason,
            resource_count=len(resources),
            skipped_count=counters.skipped_count,
            tool_call_count=0,
        )
        return self._execution(step, context, status, reason, resources, evidence_items, (operation,), counters.skipped_count)

    def inventory(self, context: Stage2ExecutionContext) -> VaultInventory:
        """Return safe counters and an aggregate content hash, never a path list."""
        operation_id = f"{context.route_step.step_id}-01-vault-view-search"
        step = ManifestStep(
            step_id=operation_id,
            route_step_id=context.route_step.step_id,
            executor="core",
            adapter="obsidian-local",
            operation=Stage2Operation.VAULT_VIEW_SEARCH,
            arguments={
                "vault_id": context.config.obsidian.vault_id,
                "view_tag": context.route_step.view_tag,
                "query": "inventory",
                "scope_sha256": context.config.obsidian.scope_sha256,
            },
            on_empty=context.route_step.on_empty,
            on_unavailable=context.route_step.on_unavailable,
        )
        counters = VaultCounters()
        scope = self._io.prepare_scope(context.config.obsidian)
        candidates = tuple(self._io.iter_markdown(scope, counters))
        for candidate in candidates:
            try:
                verified = self._io.open_verified(candidate, scope)
                parsed = self._parser.parse(verified.raw_bytes, candidate.relative_path)
                counters.content_hashes.append((candidate.relative_path, verified.content_sha256))
                if parsed.classification == "unmanaged":
                    counters.unmanaged_count += 1
                elif parsed.classification == "conflict":
                    counters.conflict_count += 1
                else:
                    counters.matched_count += 1
            except (ObsidianMarkdownError, MtrpError):
                counters.error_count += 1
                counters.skipped_count += 1
        return VaultInventory(
            scanned_count=counters.scanned_count,
            matched_count=counters.matched_count,
            unmanaged_count=counters.unmanaged_count,
            conflict_count=counters.conflict_count,
            skipped_count=counters.skipped_count,
            error_count=counters.error_count,
            excluded_count=counters.excluded_count,
            symlink_count=counters.symlink_count,
            aggregate_sha256=sha256_json(sorted(counters.content_hashes)),
        )

    def _canonical(
        self,
        step: ManifestStep,
        context: Stage2ExecutionContext,
        path: str,
        title: str,
        content_sha256: str,
    ) -> tuple[ResourceV2, EvidenceV2]:
        source = SourceRef(
            store=context.route_step.store,
            store_id=context.route_step.store_id,
            logical_key=f"{context.config.obsidian.vault_id}:{path}",
        )
        snapshot = SnapshotRef(kind=SnapshotKind.CONTENT_SHA256, value=content_sha256)
        evidence_id = identity.evidence_id(
            {
                "source": source,
                "snapshot": snapshot,
                "content_sha256": content_sha256,
                "request_id": context.manifest.request_id,
                "route_id": context.manifest.route_id,
                "execution_id": context.manifest.execution_id,
                "trace_id": context.trace_id,
                "step_id": context.route_step.step_id,
                "operation_id": step.step_id,
            }
        )
        evidence = EvidenceV2(
            evidence_id=evidence_id,
            source_ref=source,
            snapshot_ref=snapshot,
            path=path,
            content_sha256=content_sha256,
            request_hash=context.manifest.request_hash,
            plan_hash=context.manifest.plan_hash,
            step_id=context.route_step.step_id,
            operation_id=step.step_id,
            request_id=context.manifest.request_id,
            route_id=context.manifest.route_id,
            execution_id=context.manifest.execution_id,
            trace_id=context.trace_id,
            content_hash_scope="raw_bytes",
            content_format="markdown",
        )
        resource_id = identity.resource_id(
            {
                "source": source,
                "snapshot": snapshot,
                "content_sha256": content_sha256,
                "step_id": context.route_step.step_id,
                "operation_id": step.step_id,
                "trace_id": context.trace_id,
            }
        )
        resource = ResourceV2(
            resource_id=resource_id,
            resource_type=ResourceType.VAULT_NOTE,
            title=title,
            content_ref=identity.content_ref({"content_sha256": content_sha256, "source": source}),
            source_ref=source,
            snapshot_ref=snapshot,
            evidence_ids=(evidence_id,),
            score=None,
            score_source="not_provided",
            trace_id=context.trace_id,
            step_id=context.route_step.step_id,
            operation_id=step.step_id,
        )
        return resource, evidence

    def _empty_or_degraded(
        self,
        step: ManifestStep,
        context: Stage2ExecutionContext,
        counters: VaultCounters,
        reason_code: str,
    ) -> StepExecutionV2:
        empty = reason_code != "VAULT_SCAN_LIMIT_EXCEEDED" and counters.error_count == 0
        status = StepStatusV2.EMPTY if empty else StepStatusV2.DEGRADED
        reason = ("T_EMPTY" if context.route_step.store is StoreKind.T else "R_EMPTY") if empty else reason_code
        operation = OperationResult(
            step_id=step.step_id,
            status=status,
            reason_code=reason,
            skipped_count=counters.skipped_count,
            tool_call_count=0,
        )
        return self._execution(step, context, status, reason, [], [], (operation,), counters.skipped_count)

    @staticmethod
    def _execution(
        step: ManifestStep,
        context: Stage2ExecutionContext,
        status: StepStatusV2,
        reason: str | None,
        resources: list[ResourceV2],
        evidence: list[EvidenceV2],
        operations: tuple[OperationResult, ...],
        skipped_count: int,
    ) -> StepExecutionV2:
        result = StepResultV2(
            step_id=context.route_step.step_id,
            store=context.route_step.store,
            status=status,
            reason_code=reason,
            resource_ids=tuple(item.resource_id for item in resources),
            evidence_ids=tuple(item.evidence_id for item in evidence),
            resource_count=len(resources),
            evidence_count=len(evidence),
            request_id=context.manifest.request_id,
            route_id=context.manifest.route_id,
            execution_id=context.manifest.execution_id,
            trace_id=context.trace_id,
            operations=operations,
            skipped_count=skipped_count,
        )
        return StepExecutionV2(step_result=result, resources=tuple(resources), evidence=tuple(evidence))


__all__ = [
    "FileIdentity",
    "ObsidianAdapter",
    "PreparedVaultScope",
    "SafeVaultIO",
    "VaultCandidate",
    "VaultCounters",
    "VaultInventory",
    "VerifiedVaultFile",
]
