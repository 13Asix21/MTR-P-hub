"""Validated v1 routing semantics; YAML cannot silently change the main route."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field

from mtrp_hub.canonical import sha256_json
from mtrp_hub.config.schemas import RoutingConfig
from mtrp_hub.domain.enums import EmptyAction, Intent, ReadOperation, StoreKind
from mtrp_hub.domain.models import FrozenModel
from mtrp_hub.errors import configuration_error


class PolicyStep(FrozenModel):
    store: StoreKind
    operation: ReadOperation


class IntentPolicy(FrozenModel):
    intent: Intent
    implicit_r: bool = False
    steps: tuple[PolicyStep, ...] = Field(min_length=1)


class TerminalPolicy(FrozenModel):
    on_empty: EmptyAction
    on_unavailable: EmptyAction


class OptionalRPolicy(FrozenModel):
    position: str
    operation: ReadOperation


class RoutingPolicy(FrozenModel):
    """Complete, canonical policy object used for hashing and planning."""

    schema_version: str
    rule_version: str
    intents: tuple[IntentPolicy, ...]
    terminal: TerminalPolicy
    optional_r: OptionalRPolicy

    _V1_ROUTES: ClassVar[dict[Intent, tuple[tuple[StoreKind, ReadOperation], ...]]] = {
        Intent.PROJECT_CONTEXT: ((StoreKind.P, ReadOperation.PROJECT_READ), (StoreKind.M, ReadOperation.KNOWLEDGE_SEARCH), (StoreKind.T, ReadOperation.VAULT_VIEW_SEARCH)),
        Intent.GENERAL_KNOWLEDGE: ((StoreKind.M, ReadOperation.KNOWLEDGE_SEARCH), (StoreKind.T, ReadOperation.VAULT_VIEW_SEARCH)),
        Intent.METHOD_SKILL: ((StoreKind.M, ReadOperation.KNOWLEDGE_SEARCH), (StoreKind.T, ReadOperation.VAULT_VIEW_SEARCH)),
        Intent.DECISION_HISTORY: ((StoreKind.P, ReadOperation.ADR_SEARCH), (StoreKind.T, ReadOperation.VAULT_VIEW_SEARCH), (StoreKind.M, ReadOperation.KNOWLEDGE_SEARCH)),
        Intent.COLD_DETAIL: ((StoreKind.R, ReadOperation.VAULT_VIEW_SEARCH), (StoreKind.T, ReadOperation.VAULT_VIEW_SEARCH)),
    }

    @classmethod
    def from_config(cls, config: RoutingConfig) -> "RoutingPolicy":
        """Validate the full v1 contract, not merely the version label."""
        if config.schema_version != "1":
            raise configuration_error("RULE_VERSION_UNSUPPORTED", "routing.schema_version", "Unsupported routing schema.")
        if config.rule_version != "v1":
            raise configuration_error("RULE_VERSION_UNSUPPORTED", "routing.rule_version", "Only routing rule v1 is implemented.")
        if set(config.intents) != {intent.value for intent in Intent}:
            raise configuration_error("ROUTING_SEMANTICS_INVALID", "routing.intents", "v1 requires exactly the five supported intents.")
        if config.terminal.on_empty is not EmptyAction.RETURN_EMPTY or config.terminal.on_unavailable is not EmptyAction.RETURN_EMPTY:
            raise configuration_error("ROUTING_SEMANTICS_INVALID", "routing.terminal", "v1 terminal actions must return_empty.")
        if config.optional_r.position != "append" or config.optional_r.operation is not ReadOperation.VAULT_VIEW_SEARCH:
            raise configuration_error("ROUTING_SEMANTICS_INVALID", "routing.optional_r", "v1 optional R must append a Vault view search.")
        intents: list[IntentPolicy] = []
        for intent in Intent:
            raw = config.intents[intent.value]
            actual = tuple((step.store, step.operation) for step in raw.steps)
            if actual != cls._V1_ROUTES[intent]:
                raise configuration_error(
                    "ROUTING_SEMANTICS_INVALID",
                    f"routing.intents.{intent.value}.steps",
                    "YAML steps differ from the implemented v1 route.",
                )
            expected_implicit = intent is Intent.COLD_DETAIL
            if raw.implicit_r != expected_implicit:
                raise configuration_error(
                    "ROUTING_SEMANTICS_INVALID",
                    f"routing.intents.{intent.value}.implicit_r",
                    "implicit_r does not match the v1 contract.",
                )
            intents.append(
                IntentPolicy(
                    intent=intent,
                    implicit_r=raw.implicit_r,
                    steps=tuple(PolicyStep(store=step.store, operation=step.operation) for step in raw.steps),
                )
            )
        return cls(
            schema_version=config.schema_version,
            rule_version=config.rule_version,
            intents=tuple(intents),
            terminal=TerminalPolicy(on_empty=config.terminal.on_empty, on_unavailable=config.terminal.on_unavailable),
            optional_r=OptionalRPolicy(position=config.optional_r.position, operation=config.optional_r.operation),
        )

    def for_intent(self, intent: Intent) -> IntentPolicy:
        return next(item for item in self.intents if item.intent is intent)

    def fingerprint(self) -> str:
        return sha256_json(self)
