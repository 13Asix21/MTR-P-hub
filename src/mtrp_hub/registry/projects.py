"""Project/Repository/Location/Workspace indexes and default coordinate resolution."""

from __future__ import annotations

from types import MappingProxyType
from typing import Iterable, Mapping

from mtrp_hub.canonical import sha256_json
from mtrp_hub.config.schemas import ProjectsConfig
from mtrp_hub.domain.enums import LocationKind, LocationRole, RepositoryState
from mtrp_hub.domain.models import (
    Location,
    Project,
    ProjectCoordinate,
    Repository,
    Workspace,
)
from mtrp_hub.errors import MtrpError, configuration_error


class ProjectRegistry:
    """Immutable in-memory Registry; Workspace roots are metadata, never probed."""

    __slots__ = ("_projects", "_projects_by_id", "_repositories", "_workspaces")

    def __init__(self, projects: Iterable[Project]) -> None:
        ordered_projects = tuple(sorted(projects, key=lambda item: item.project_id))
        projects_by_id: dict[str, Project] = {}
        repositories: dict[tuple[str, str], Repository] = {}
        workspaces: dict[tuple[str, str, str], tuple[Workspace, Location]] = {}
        for project in ordered_projects:
            if project.project_id in projects_by_id:
                raise configuration_error("DUPLICATE_ID", f"projects.{project.project_id}", "Duplicate Project ID.")
            projects_by_id[project.project_id] = project
            repo_ids: set[str] = set()
            for repository in sorted(project.repositories, key=lambda item: item.repo_id):
                if repository.repo_id in repo_ids:
                    raise configuration_error(
                        "DUPLICATE_ID",
                        f"projects.{project.project_id}.{repository.repo_id}",
                        "Duplicate Repository ID within Project.",
                    )
                repo_ids.add(repository.repo_id)
                self._validate_repository(project.project_id, repository)
                repositories[(project.project_id, repository.repo_id)] = repository
                location_by_id = {item.location_id: item for item in repository.locations}
                for workspace in repository.workspaces:
                    key = (project.project_id, repository.repo_id, workspace.workspace_id)
                    if key in workspaces:
                        raise configuration_error("DUPLICATE_ID", f"workspaces.{workspace.workspace_id}", "Duplicate Workspace ID.")
                    location = location_by_id[workspace.location_id]
                    if location.kind is not LocationKind.LOCAL_GIT:
                        raise configuration_error(
                            "UNKNOWN_REFERENCE",
                            f"workspaces.{workspace.workspace_id}.location_id",
                            "Workspace must reference a LocalGit Location.",
                        )
                    workspaces[key] = (workspace, location)
        self._projects = ordered_projects
        self._projects_by_id = MappingProxyType(projects_by_id)
        self._repositories = MappingProxyType(repositories)
        self._workspaces = MappingProxyType(workspaces)

    @staticmethod
    def _validate_repository(project_id: str, repository: Repository) -> None:
        location_ids = [item.location_id for item in repository.locations]
        if len(set(location_ids)) != len(location_ids):
            raise configuration_error("DUPLICATE_ID", f"repositories.{repository.repo_id}.locations", "Duplicate Location ID.")
        authoritative = [item for item in repository.locations if item.role is LocationRole.AUTHORITATIVE]
        if (
            len(authoritative) != 1
            or authoritative[0].location_id != repository.authoritative_location_id
        ):
            raise configuration_error(
                "AUTHORITATIVE_LOCATION_INVALID",
                f"projects.{project_id}.repositories.{repository.repo_id}.authoritative_location_id",
                "Repository requires exactly one matching authoritative Location.",
            )
        if repository.authoritative_location_id not in location_ids:
            raise configuration_error("UNKNOWN_REFERENCE", "authoritative_location_id", "Authoritative Location is unknown.")
        location_ids_set = set(location_ids)
        workspace_ids = [item.workspace_id for item in repository.workspaces]
        if len(set(workspace_ids)) != len(workspace_ids):
            raise configuration_error("DUPLICATE_ID", f"repositories.{repository.repo_id}.workspaces", "Duplicate Workspace ID.")
        for workspace in repository.workspaces:
            if workspace.location_id not in location_ids_set:
                raise configuration_error("UNKNOWN_REFERENCE", f"workspaces.{workspace.workspace_id}.location_id", "Workspace Location is unknown.")

    @classmethod
    def from_config(cls, config: ProjectsConfig) -> "ProjectRegistry":
        projects: list[Project] = []
        for project_config in config.projects:
            repositories: list[Repository] = []
            for repo_config in project_config.repositories:
                repositories.append(
                    Repository(
                        repo_id=repo_config.repo_id,
                        default_branch=repo_config.default_branch,
                        authoritative_location_id=repo_config.authoritative_location_id,
                        adr_paths=repo_config.adr_paths,
                        locations=tuple(Location(**item.model_dump()) for item in repo_config.locations),
                        workspaces=tuple(Workspace(**item.model_dump()) for item in repo_config.workspaces),
                    )
                )
            projects.append(Project(project_id=project_config.project_id, repositories=tuple(repositories)))
        return cls(projects)

    @property
    def projects(self) -> tuple[Project, ...]:
        return self._projects

    def _repository(self, project_id: str, repo_id: str) -> Repository:
        if project_id not in self._projects_by_id:
            raise MtrpError("UNKNOWN_PROJECT", "Project ID is not registered.", field="project")
        try:
            return self._repositories[(project_id, repo_id)]
        except KeyError as exc:
            raise MtrpError("UNKNOWN_REPOSITORY", "Repository ID is not registered for the Project.", field="repo") from exc

    @staticmethod
    def _commit_for(repository: Repository, location: Location) -> str | None:
        if location.state in (RepositoryState.EMPTY, RepositoryState.UNKNOWN):
            return None
        return next((pin.commit for pin in location.snapshots if pin.ref == repository.default_branch), None)

    def resolve_default(self, project_id: str, repo_id: str) -> ProjectCoordinate:
        """Resolve the authoritative default branch without inventing a snapshot."""
        repository = self._repository(project_id, repo_id)
        location = next(item for item in repository.locations if item.location_id == repository.authoritative_location_id)
        workspace_id: str | None = None
        if location.kind is LocationKind.LOCAL_GIT:
            matching = [item for item in repository.workspaces if item.location_id == location.location_id]
            if len(matching) != 1:
                raise configuration_error(
                    "AUTHORITATIVE_LOCATION_INVALID",
                    f"repositories.{repo_id}.workspaces",
                    "A LocalGit authoritative Location requires exactly one registered Workspace.",
                )
            workspace_id = matching[0].workspace_id
        return ProjectCoordinate(
            project_id=project_id,
            repo_id=repo_id,
            location_id=location.location_id,
            location_kind=location.kind,
            ref=repository.default_branch,
            commit=self._commit_for(repository, location),
            path=None,
            workspace_id=workspace_id,
            repository_state=location.state,
        )

    def resolve_workspace(self, project_id: str, repo_id: str, workspace_id: str) -> ProjectCoordinate:
        """Resolve only a registered LocalGit Workspace; never stat or open its root."""
        repository = self._repository(project_id, repo_id)
        try:
            workspace, location = self._workspaces[(project_id, repo_id, workspace_id)]
        except KeyError as exc:
            raise MtrpError("UNREGISTERED_WORKSPACE", "Workspace ID is not registered.", field="workspace") from exc
        return ProjectCoordinate(
            project_id=project_id,
            repo_id=repo_id,
            location_id=location.location_id,
            location_kind=location.kind,
            ref=repository.default_branch,
            commit=self._commit_for(repository, location),
            path=None,
            workspace_id=workspace.workspace_id,
            repository_state=location.state,
        )

    def get_adr_paths(self, project_id: str, repo_id: str) -> tuple[str, ...]:
        """Return safe configured ADR roots; request-supplied paths are never accepted."""
        return self._repository(project_id, repo_id).adr_paths

    def fingerprint_payload(self) -> list[dict[str, object]]:
        """Return sorted non-secret Registry DTOs, excluding remotes and local roots."""
        payload: list[dict[str, object]] = []
        for project in self._projects:
            repos: list[dict[str, object]] = []
            for repository in sorted(project.repositories, key=lambda item: item.repo_id):
                locations = []
                for location in sorted(repository.locations, key=lambda item: item.location_id):
                    locations.append(
                        {
                            "location_id": location.location_id,
                            "kind": location.kind.value,
                            "role": location.role.value,
                            "state": location.state.value,
                            "snapshots": [pin.model_dump(mode="json") for pin in sorted(location.snapshots, key=lambda item: item.ref)],
                        }
                    )
                repos.append(
                    {
                        "repo_id": repository.repo_id,
                        "default_branch": repository.default_branch,
                        "authoritative_location_id": repository.authoritative_location_id,
                        "adr_paths": list(repository.adr_paths),
                        "locations": locations,
                        "workspaces": [
                            {"workspace_id": item.workspace_id, "location_id": item.location_id}
                            for item in sorted(repository.workspaces, key=lambda item: item.workspace_id)
                        ],
                    }
                )
            payload.append({"project_id": project.project_id, "repositories": repos})
        return payload

    def fingerprint(self) -> str:
        return sha256_json(self.fingerprint_payload())
