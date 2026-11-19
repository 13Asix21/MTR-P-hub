"""Configuration DTO and Store/Project Registry validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from mtrp_hub.config.loader import ConfigLoader
from mtrp_hub.domain.enums import LocationKind, LocationRole, RepositoryState, StoreKind
from mtrp_hub.domain.models import Location, Project, ProjectCoordinate, Repository, StoreDefinition, Workspace
from mtrp_hub.errors import MtrpError
from mtrp_hub.registry.projects import ProjectRegistry
from mtrp_hub.registry.stores import StoreRegistry


def test_examples_build_registries_and_unknown_default_snapshot(config_dir: Path) -> None:
    loaded = ConfigLoader().load(str(config_dir))
    coordinate = loaded.projects.resolve_default("example-project", "example-repo")
    assert coordinate.location_id == "remote-main"
    assert coordinate.ref == "main"
    assert coordinate.commit is None
    assert coordinate.workspace_id is None
    assert loaded.projects.get_adr_paths("example-project", "example-repo") == ("docs/adr",)
    assert len(loaded.registry_hash) == 64
    assert len(loaded.policy_hash) == 64


def test_store_registry_rejects_duplicate_kind_and_non_fake() -> None:
    stores = [StoreDefinition(store_id="p", kind=StoreKind.P, adapter="fake", read_only=True)]
    stores.append(StoreDefinition(store_id="other", kind=StoreKind.P, adapter="fake", read_only=True))
    with pytest.raises(MtrpError, match="Duplicate"):
        StoreRegistry(stores)
    with pytest.raises(MtrpError):
        StoreRegistry([StoreDefinition(store_id="p", kind=StoreKind.P, adapter="github", read_only=True)])


def repository_with_two_authoritative() -> Repository:
    return Repository(
        repo_id="example-repo",
        default_branch="main",
        authoritative_location_id="one",
        locations=(
            Location(location_id="one", kind=LocationKind.GITHUB, role=LocationRole.AUTHORITATIVE),
            Location(location_id="two", kind=LocationKind.GITHUB, role=LocationRole.AUTHORITATIVE),
        ),
    )


def test_project_registry_rejects_multiple_authoritative_locations() -> None:
    with pytest.raises(MtrpError) as caught:
        ProjectRegistry([Project(project_id="example-project", repositories=(repository_with_two_authoritative(),))])
    assert caught.value.error_code == "AUTHORITATIVE_LOCATION_INVALID"


def test_unknown_project_repository_and_workspace(config_dir: Path) -> None:
    registry = ConfigLoader().load(str(config_dir)).projects
    with pytest.raises(MtrpError) as caught:
        registry.resolve_default("unknown", "example-repo")
    assert caught.value.error_code == "UNKNOWN_PROJECT"
    with pytest.raises(MtrpError) as caught:
        registry.resolve_default("example-project", "unknown")
    assert caught.value.error_code == "UNKNOWN_REPOSITORY"
    with pytest.raises(MtrpError) as caught:
        registry.resolve_workspace("example-project", "example-repo", "unknown")
    assert caught.value.error_code == "UNREGISTERED_WORKSPACE"


def test_registered_workspace_resolution_is_metadata_only(tmp_path: Path) -> None:
    missing_root = tmp_path / "never-probed"
    repository = Repository(
        repo_id="example-repo",
        default_branch="main",
        authoritative_location_id="remote-main",
        locations=(
            Location(location_id="remote-main", kind=LocationKind.GITHUB, role=LocationRole.AUTHORITATIVE),
            Location(location_id="local", kind=LocationKind.LOCAL_GIT, role=LocationRole.WORKSPACE),
        ),
        workspaces=(Workspace(workspace_id="local-workspace", location_id="local", root_path=str(missing_root)),),
    )
    registry = ProjectRegistry([Project(project_id="example-project", repositories=(repository,))])
    coordinate = registry.resolve_workspace("example-project", "example-repo", "local-workspace")
    assert coordinate.location_kind is LocationKind.LOCAL_GIT
    assert coordinate.workspace_id == "local-workspace"
    assert not missing_root.exists()


def test_v1_yaml_cannot_change_route_without_version(config_dir: Path) -> None:
    routing = config_dir / "routing.yaml"
    routing.write_text(routing.read_text(encoding="utf-8").replace("P → M → T", "unused"), encoding="utf-8")
    text = routing.read_text(encoding="utf-8")
    text = text.replace(
        "      - {store: M, operation: knowledge_search}\n      - {store: T, operation: vault_view_search}\n  general_knowledge:",
        "      - {store: T, operation: vault_view_search}\n      - {store: M, operation: knowledge_search}\n  general_knowledge:",
        1,
    )
    routing.write_text(text, encoding="utf-8")
    with pytest.raises(MtrpError) as caught:
        ConfigLoader().load(str(config_dir))
    assert caught.value.error_code == "ROUTING_SEMANTICS_INVALID"


def test_missing_config_is_safe_error(tmp_path: Path) -> None:
    with pytest.raises(MtrpError) as caught:
        ConfigLoader().load(str(tmp_path))
    assert caught.value.error_code == "CONFIG_NOT_FOUND"
    assert str(tmp_path) not in caught.value.safe_message
