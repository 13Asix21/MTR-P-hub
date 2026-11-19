"""Store uniqueness, fixed Fake/read-only constraints, and safe fingerprints."""

from __future__ import annotations

from types import MappingProxyType
from typing import Iterable, Mapping

from mtrp_hub.canonical import sha256_json
from mtrp_hub.config.schemas import StoresConfig
from mtrp_hub.domain.enums import StoreKind
from mtrp_hub.domain.models import StoreDefinition
from mtrp_hub.errors import configuration_error


class StoreRegistry:
    """Immutable Registry containing exactly one P, M, T, and R definition."""

    __slots__ = ("_by_id", "_by_kind", "_stores")

    def __init__(self, stores: Iterable[StoreDefinition]) -> None:
        ordered = tuple(sorted(stores, key=lambda item: item.store_id))
        by_id: dict[str, StoreDefinition] = {}
        by_kind: dict[StoreKind, StoreDefinition] = {}
        for store in ordered:
            if store.store_id in by_id:
                raise configuration_error("DUPLICATE_ID", f"stores.{store.store_id}", "Duplicate Store ID.")
            if store.kind in by_kind:
                raise configuration_error("DUPLICATE_ID", f"stores.{store.kind.value}", "Duplicate logical Store kind.")
            if store.adapter != "fake" or not store.read_only:
                raise configuration_error(
                    "STORE_CONSTRAINT_INVALID",
                    f"stores.{store.store_id}",
                    "Stage 0+1 Stores must be read-only Fake adapters.",
                )
            if store.kind in (StoreKind.T, StoreKind.R):
                expected = "mtr/trial" if store.kind is StoreKind.T else "mtr/rarely"
                if store.vault_id is None or store.view_tag != expected:
                    raise configuration_error(
                        "STORE_CONSTRAINT_INVALID",
                        f"stores.{store.store_id}",
                        "T/R Stores require the same Vault and their fixed MTR view tag.",
                    )
            elif store.vault_id is not None or store.view_tag is not None:
                raise configuration_error(
                    "STORE_CONSTRAINT_INVALID",
                    f"stores.{store.store_id}",
                    "P/M Stores cannot declare Vault IDs or view tags.",
                )
            by_id[store.store_id] = store
            by_kind[store.kind] = store
        if set(by_kind) != set(StoreKind):
            raise configuration_error("UNKNOWN_REFERENCE", "stores", "Exactly one P/M/T/R Store is required.")
        vault_ids = {by_kind[kind].vault_id for kind in (StoreKind.T, StoreKind.R)}
        if len(vault_ids) != 1:
            raise configuration_error("STORE_CONSTRAINT_INVALID", "stores", "T and R must share one logical Vault.")
        self._stores = ordered
        self._by_id = MappingProxyType(by_id)
        self._by_kind = MappingProxyType(by_kind)

    @classmethod
    def from_config(cls, config: StoresConfig) -> "StoreRegistry":
        """Build validated domain entities from raw YAML DTOs."""
        return cls(StoreDefinition(**item.model_dump()) for item in config.stores)

    @property
    def stores(self) -> tuple[StoreDefinition, ...]:
        return self._stores

    def get(self, kind: StoreKind) -> StoreDefinition:
        """Return a logical Store or raise a safe configuration error."""
        try:
            return self._by_kind[kind]
        except KeyError as exc:
            raise configuration_error("UNKNOWN_REFERENCE", f"stores.{kind.value}", "Logical Store is not registered.") from exc

    def get_by_id(self, store_id: str) -> StoreDefinition:
        try:
            return self._by_id[store_id]
        except KeyError as exc:
            raise configuration_error("UNKNOWN_REFERENCE", "stores", "Store ID is not registered.") from exc

    def fingerprint_payload(self) -> list[dict[str, object]]:
        """Return sorted non-secret DTOs suitable for canonical hashing."""
        return [store.model_dump(mode="json") for store in self._stores]

    def fingerprint(self) -> str:
        return sha256_json(self.fingerprint_payload())
