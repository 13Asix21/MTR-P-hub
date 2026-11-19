"""Read exactly three explicit YAML files and build validated immutable policies."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from mtrp_hub.config.schemas import ProjectsConfig, RoutingConfig, StoresConfig
from mtrp_hub.errors import MtrpError, configuration_error, safe_validation_field
from mtrp_hub.registry.projects import ProjectRegistry
from mtrp_hub.registry.stores import StoreRegistry
from mtrp_hub.routing.policy import RoutingPolicy


@dataclass(frozen=True)
class LoadedConfig:
    """Validated cross-file configuration consumed by the application service."""

    stores: StoreRegistry
    projects: ProjectRegistry
    policy: RoutingPolicy

    @property
    def registry_hash(self) -> str:
        from mtrp_hub.canonical import sha256_json

        return sha256_json(
            {
                "stores": self.stores.fingerprint_payload(),
                "projects": self.projects.fingerprint_payload(),
            }
        )

    @property
    def policy_hash(self) -> str:
        return self.policy.fingerprint()


ConfigT = TypeVar("ConfigT", bound=BaseModel)


class ConfigLoader:
    """Explicit bootstrap loader with no parent/home/environment discovery."""

    _FILENAMES = ("stores.yaml", "projects.yaml", "routing.yaml")

    def load(self, config_dir: str) -> LoadedConfig:
        """Load only the three fixed filenames under the explicit directory."""
        root = Path(config_dir)
        stores_data = self._read_yaml(root / self._FILENAMES[0])
        projects_data = self._read_yaml(root / self._FILENAMES[1])
        routing_data = self._read_yaml(root / self._FILENAMES[2])
        stores_config = self._validate(StoresConfig, stores_data, "stores")
        projects_config = self._validate(ProjectsConfig, projects_data, "projects")
        routing_config = self._validate(RoutingConfig, routing_data, "routing")
        return LoadedConfig(
            stores=StoreRegistry.from_config(stores_config),
            projects=ProjectRegistry.from_config(projects_config),
            policy=RoutingPolicy.from_config(routing_config),
        )

    @staticmethod
    def _read_yaml(path: Path) -> Any:
        try:
            with path.open("r", encoding="utf-8") as stream:
                data = YAML(typ="safe").load(stream)
        except FileNotFoundError as exc:
            raise MtrpError("CONFIG_NOT_FOUND", "A required configuration file is missing.", field=path.name) from exc
        except (OSError, UnicodeError) as exc:
            raise MtrpError("CONFIG_NOT_FOUND", "A required configuration file cannot be read.", field=path.name) from exc
        except YAMLError as exc:
            raise MtrpError("CONFIG_INVALID", "Configuration YAML cannot be parsed.", field=path.name) from exc
        if not isinstance(data, dict):
            raise MtrpError("CONFIG_INVALID", "Configuration root must be a mapping.", field=path.name)
        ConfigLoader._ensure_finite_acyclic(data, path.name)
        return data

    @staticmethod
    def _ensure_finite_acyclic(value: Any, field: str) -> None:
        active: set[int] = set()

        def visit(item: Any) -> None:
            if isinstance(item, float) and not math.isfinite(item):
                raise MtrpError("CONFIG_INVALID", "Configuration contains a non-finite number.", field=field)
            if isinstance(item, (dict, list, tuple)):
                identity = id(item)
                if identity in active:
                    raise MtrpError("CONFIG_INVALID", "Recursive YAML aliases are not allowed.", field=field)
                active.add(identity)
                if isinstance(item, dict):
                    for key, child in item.items():
                        if not isinstance(key, str):
                            raise MtrpError("CONFIG_INVALID", "Configuration mapping keys must be strings.", field=field)
                        visit(child)
                else:
                    for child in item:
                        visit(child)
                active.remove(identity)

        visit(value)

    @staticmethod
    def _validate(model: type[ConfigT], data: Any, field: str) -> ConfigT:
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            safe_field = safe_validation_field(
                field,
                exc.errors(include_url=False, include_input=False),
            )
            raise configuration_error("CONFIG_INVALID", safe_field, "Configuration schema validation failed.") from exc
