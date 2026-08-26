from __future__ import annotations

from collections.abc import Iterable
from importlib.metadata import entry_points

from agent_ops.deployment.models import DeploymentProvider


def normalize_deployment_providers(
    providers: Iterable[DeploymentProvider],
) -> tuple[DeploymentProvider, ...]:
    """Validate installed providers and return a deterministic immutable set."""
    normalized: list[DeploymentProvider] = []
    provider_ids: set[str] = set()
    for provider in providers:
        if not isinstance(provider, DeploymentProvider):
            raise TypeError("deployment provider does not implement the runtime protocol")
        provider_id = provider.provider_id
        if type(provider_id) is not str or not provider_id.strip():
            raise ValueError("deployment provider id must be a nonempty string")
        if provider_id != provider_id.strip():
            raise ValueError("deployment provider id must not contain surrounding whitespace")
        if provider_id in provider_ids:
            raise ValueError(f"duplicate deployment provider id: {provider_id}")
        provider_ids.add(provider_id)
        normalized.append(provider)
    return tuple(sorted(normalized, key=lambda provider: provider.provider_id))


def load_deployment_providers() -> list[DeploymentProvider]:
    providers: list[DeploymentProvider] = []
    discovered = sorted(
        entry_points(group="agent_ops.deployment_providers"),
        key=lambda entry_point: (entry_point.name, entry_point.value),
    )
    for entry_point in discovered:
        try:
            providers.append(entry_point.load()())
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise RuntimeError(
                f"failed to load deployment provider {entry_point.name!r}"
            ) from error
    return list(normalize_deployment_providers(providers))
