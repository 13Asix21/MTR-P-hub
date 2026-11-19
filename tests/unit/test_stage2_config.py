"""Stage 2 fourth-file, G1, Registry binding, and audit containment tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from mtrp_hub.config.stage2 import ObsidianStage2Config, Stage2Config, Stage2ConfigLoader
from mtrp_hub.errors import MtrpError


def yaml_data(config_dir: Path) -> dict:
    return YAML(typ="safe").load((config_dir / "stage2.yaml").read_text(encoding="utf-8"))


def test_example_is_fixture_safe_and_hashes_all_execution_semantics(config_dir: Path) -> None:
    loaded = Stage2ConfigLoader().load(str(config_dir))
    assert loaded.obsidian.mode == "fixture"
    assert loaded.obsidian.g1_confirmed is False
    assert loaded.workbuddy_manifest.persist_raw_responses is False
    assert len(loaded.config.execution_config_hash) == 64
    assert loaded.github_p.repositories[0].read_paths == ("README.md",)


def test_missing_fourth_file_never_falls_back(config_dir: Path) -> None:
    (config_dir / "stage2.yaml").unlink()
    with pytest.raises(MtrpError) as caught:
        Stage2ConfigLoader().load(str(config_dir))
    assert caught.value.error_code == "STAGE2_CONFIG_NOT_FOUND"


def test_extra_fields_and_unsafe_flags_fail_closed(config_dir: Path) -> None:
    data = yaml_data(config_dir)
    data["obsidian"]["follow_symlinks"] = True
    with pytest.raises(ValidationError):
        Stage2Config.model_validate(data)
    data = yaml_data(config_dir)
    data["unexpected_network"] = True
    with pytest.raises(ValidationError):
        Stage2Config.model_validate(data)


def test_real_mode_requires_g1_absolute_exact_scope(config_dir: Path) -> None:
    data = yaml_data(config_dir)
    data["obsidian"]["mode"] = "obsidian-local"
    data["obsidian"]["vault_root"] = str(config_dir.parent / "vault")
    data["lexiang_m"]["mode"] = "workbuddy-manifest"
    data["github_p"]["mode"] = "workbuddy-manifest"
    with pytest.raises(ValidationError):
        Stage2Config.model_validate(data)


def test_audit_and_config_cannot_be_inside_vault(config_dir: Path, tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    config = ObsidianStage2Config(
        mode="obsidian-local",
        vault_id="fixture-vault",
        vault_root=str(vault),
        managed_roots=({"root_id": "permanent", "path": "2-Permanent"},),
        excluded_roots=(
            "2-Permanent/Resources/MTR-P-System",
            "2-Permanent/Areas/AI-Assets/_templates",
        ),
        g1_confirmed=True,
    )
    with pytest.raises(MtrpError) as caught:
        Stage2ConfigLoader.ensure_audit_outside_vault(vault / "config", tmp_path / "audit.sqlite3", config)
    assert caught.value.error_code == "CONFIG_INSIDE_VAULT"
    with pytest.raises(MtrpError) as caught:
        Stage2ConfigLoader.ensure_audit_outside_vault(config_dir, vault / ".mtrp" / "decision.sqlite3", config)
    assert caught.value.error_code == "AUDIT_INSIDE_VAULT"


def test_excluded_parent_cannot_cover_managed_root_and_segments_are_exact() -> None:
    with pytest.raises(ValidationError):
        ObsidianStage2Config(
            vault_id="fixture-vault",
            vault_root="tests/fixtures/stage2/vault",
            managed_roots=({"root_id": "nested", "path": "A/B"},),
            excluded_roots=("A",),
        )
    from mtrp_hub.adapters.obsidian import SafeVaultIO

    config = ObsidianStage2Config(
        vault_id="fixture-vault",
        vault_root="tests/fixtures/stage2/vault",
        managed_roots=({"root_id": "permanent", "path": "A"},),
        excluded_roots=("A/B",),
    )
    assert SafeVaultIO._is_excluded(config, "A/B/c.md") is True
    assert SafeVaultIO._is_excluded(config, "A/B2/c.md") is False


def test_nested_or_root_managed_scopes_are_rejected_before_lstat(monkeypatch) -> None:
    monkeypatch.setattr("os.lstat", lambda *_: (_ for _ in ()).throw(AssertionError("target I/O")))
    with pytest.raises(ValidationError):
        ObsidianStage2Config(
            vault_id="fixture-vault",
            vault_root="tests/fixtures/stage2/vault",
            managed_roots=(
                {"root_id": "permanent", "path": "2-Permanent"},
                {"root_id": "nested", "path": "2-Permanent/nested"},
            ),
        )
