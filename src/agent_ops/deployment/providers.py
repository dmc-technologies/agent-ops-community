from __future__ import annotations

from importlib.metadata import entry_points

from agent_ops.deployment.models import DeploymentProvider


def load_deployment_providers() -> list[DeploymentProvider]:
    providers: list[DeploymentProvider] = []
    for entry_point in entry_points(group="agent_ops.deployment_providers"):
        providers.append(entry_point.load()())
    return providers
