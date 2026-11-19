"""Filesystem link, NFC collision, replacement, and no-write Vault attacks."""

from __future__ import annotations

import builtins
import os
import shutil
import unicodedata
from pathlib import Path

import pytest

from mtrp_hub.adapters.obsidian import ObsidianAdapter, SafeVaultIO
from mtrp_hub.adapters.stage2_base import Stage2ExecutionContext
from mtrp_hub.application_stage2 import ManifestService
from mtrp_hub.domain.enums import Intent
from mtrp_hub.errors import MtrpError
from mtrp_hub.execution.models import ExecutionMode

from tests.conftest import ROOT


def copied_vault(tmp_path: Path, monkeypatch) -> Path:  # type: ignore[no-untyped-def]
    target = tmp_path / "tests/fixtures/stage2/vault"
    target.parent.mkdir(parents=True)
    shutil.copytree(ROOT / "tests/fixtures/stage2/vault", target)
    monkeypatch.chdir(tmp_path)
    return target


def compiled_for_cold(request_factory):  # type: ignore[no-untyped-def]
    request = request_factory(intent=Intent.COLD_DETAIL, project_id=None, repo_id=None)
    return ManifestService().compile_request(request, ExecutionMode.FIXTURE)


def test_reparse_point_target_is_never_opened_without_platform_privilege(request_factory, tmp_path: Path, monkeypatch) -> None:
    vault = copied_vault(tmp_path, monkeypatch)
    outside = tmp_path / "outside.md"
    outside.write_text("#mtr/trial\n", encoding="utf-8")
    link = vault / "2-Permanent/link.md"
    link.write_text("#mtr/trial\n", encoding="utf-8")
    original_lstat = os.lstat

    def reparse_metadata(path):  # type: ignore[no-untyped-def]
        metadata = original_lstat(path)
        if Path(path) == link:
            from types import SimpleNamespace

            return SimpleNamespace(st_mode=metadata.st_mode, st_file_attributes=0x400)
        return metadata

    monkeypatch.setattr(os, "lstat", reparse_metadata)
    opened: list[str] = []
    original = builtins.open

    def spy(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        opened.append(str(file))
        assert str(file) != str(outside)
        return original(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    compiled = compiled_for_cold(request_factory)
    route_step = compiled.route.steps[0]
    step = compiled.manifest.steps[0]
    execution = ObsidianAdapter().execute(step, Stage2ExecutionContext(compiled.config, compiled.manifest, route_step))
    assert execution.step_result.resource_count == 1
    assert str(link) not in opened and str(outside) not in opened


def test_nfc_collision_is_rejected_before_any_markdown_open(request_factory, tmp_path: Path, monkeypatch) -> None:
    vault = copied_vault(tmp_path, monkeypatch)
    root = vault / "2-Permanent"
    nfc = root / "café.md"
    nfd = root / unicodedata.normalize("NFD", "café.md")
    nfc.write_text("#mtr/trial\n", encoding="utf-8")
    try:
        nfd.write_text("#mtr/rarely\n", encoding="utf-8")
    except OSError:
        pytest.skip("filesystem does not permit distinct Unicode-normalized names")
    if nfc.read_bytes() == nfd.read_bytes() and os.path.samefile(nfc, nfd):
        pytest.skip("filesystem aliases normalized names")
    opened: list[str] = []
    original = builtins.open

    def spy(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(file).endswith(".md"):
            opened.append(str(file))
        return original(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    compiled = compiled_for_cold(request_factory)
    with pytest.raises(MtrpError) as caught:
        ObsidianAdapter().execute(
            compiled.manifest.steps[0],
            Stage2ExecutionContext(compiled.config, compiled.manifest, compiled.route.steps[0]),
        )
    assert caught.value.error_code == "VAULT_PATH_COLLISION"
    assert opened == []


def test_file_replacement_after_scan_is_fail_closed(request_factory, tmp_path: Path, monkeypatch) -> None:
    vault = copied_vault(tmp_path, monkeypatch)
    compiled = compiled_for_cold(request_factory)
    io = SafeVaultIO()
    scope = io.prepare_scope(compiled.config.obsidian)
    candidate = next(item for item in io.iter_markdown(scope) if item.relative_path.endswith("rarely.md"))
    Path(candidate.actual_path).write_text("changed after scan", encoding="utf-8")
    with pytest.raises(MtrpError) as caught:
        io.open_verified(candidate, scope)
    assert caught.value.error_code == "VAULT_FILE_CHANGED"


def test_adapter_has_no_vault_write_calls(request_factory, tmp_path: Path, monkeypatch) -> None:
    copied_vault(tmp_path, monkeypatch)
    for name in ("write_text", "write_bytes", "mkdir", "chmod", "rename", "replace", "unlink"):
        monkeypatch.setattr(Path, name, lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError(name)))
    compiled = compiled_for_cold(request_factory)
    execution = ObsidianAdapter().execute(
        compiled.manifest.steps[0],
        Stage2ExecutionContext(compiled.config, compiled.manifest, compiled.route.steps[0]),
    )
    assert execution.step_result.resource_count == 1
