from __future__ import annotations

from pathlib import Path

from agent_ops.context.models import ContextPack, ContextSource
from agent_ops.contracts.job import AgentJob
from agent_ops.registries.loader import load_capabilities, load_skills, load_tools
from agent_ops.registries.models import Framework

DEFAULT_INSTRUCTIONS = [
    "Use agent-knowledge before nontrivial work when available; prefer MCP "
    "and use AGENT_KNOWLEDGE_HOME git/file fallback when MCP is unavailable.",
    "When writing agent-knowledge entries, keep them distilled: decision/finding, "
    "rationale, paths/commands, and future implications.",
    "Use superpowers/gstack skills that match the task when available.",
    "Verify before reporting completion.",
    "Preserve golden references and avoid unrelated rewrites.",
]


def build_context_pack(
    job: AgentJob,
    framework: Framework,
    *,
    sources: list[str] | None = None,
    instructions: list[str] | None = None,
) -> ContextPack:
    capabilities = [
        capability
        for capability in load_capabilities()
        if framework in capability.frameworks
    ]
    skills = [
        skill
        for skill in load_skills()
        if any(mapping.framework == framework for mapping in skill.mappings)
    ]
    tools = [
        tool
        for tool in load_tools()
        if framework in tool.frameworks
    ]
    context_sources = [
        ContextSource(path=str(Path(source)), required=False)
        for source in (sources or [])
    ]
    return ContextPack(
        id=f"{job.id}-{framework.value}",
        framework=framework,
        job=job,
        capabilities=capabilities,
        skills=skills,
        tools=tools,
        sources=context_sources,
        instructions=[*(instructions or []), *DEFAULT_INSTRUCTIONS],
    )
