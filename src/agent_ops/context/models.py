from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from agent_ops.contracts.job import AgentJob
from agent_ops.registries.models import Capability, Framework, SkillSpec, ToolSpec


def _enum_value(value: object) -> str:
    return getattr(value, "value", str(value))


class ContextSource(BaseModel):
    path: str
    kind: str = "file"
    required: bool = False


class ContextPack(BaseModel):
    id: str
    framework: Framework
    job: AgentJob
    capabilities: list[Capability] = Field(default_factory=list)
    skills: list[SkillSpec] = Field(default_factory=list)
    tools: list[ToolSpec] = Field(default_factory=list)
    sources: list[ContextSource] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_markdown(self) -> str:
        lines = [
            f"# Context Pack: {self.id}",
            "",
            f"- Framework: `{self.framework.value}`",
            f"- Job: `{self.job.id}`",
            f"- Title: {self.job.title}",
            f"- Runner: `{_enum_value(self.job.runner)}`",
            f"- Mode: `{_enum_value(self.job.mode)}`",
            "",
            "## Instructions",
        ]
        lines.extend(f"- {instruction}" for instruction in self.instructions)
        lines.extend(["", "## Capabilities"])
        lines.extend(
            f"- `{capability.id}`: {capability.description}"
            for capability in self.capabilities
        )
        lines.extend(["", "## Skills"])
        lines.extend(f"- `{skill.id}`: {skill.description}" for skill in self.skills)
        lines.extend(["", "## Tools"])
        lines.extend(f"- `{tool.id}`: {tool.description}" for tool in self.tools)
        lines.extend(["", "## Sources"])
        lines.extend(f"- `{source.path}` ({source.kind})" for source in self.sources)
        return "\n".join(lines) + "\n"

    def write(self, output_dir: str | Path) -> tuple[Path, Path]:
        target_dir = Path(output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        json_path = target_dir / f"{self.id}.json"
        markdown_path = target_dir / f"{self.id}.md"
        json_path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        markdown_path.write_text(self.to_markdown(), encoding="utf-8")
        return json_path, markdown_path
