from __future__ import annotations

from pathlib import Path

from agent_ops.context.models import ContextPack
from agent_ops.contracts.job import AgentJob
from agent_ops.frameworks.base import AdapterCommand, FrameworkAdapter
from agent_ops.registries.models import Framework


class LocalAdapter(FrameworkAdapter):
    framework = Framework.LOCAL
    executable = "sh"

    def build_command(self, job: AgentJob, context_pack: ContextPack, cwd: Path) -> AdapterCommand:
        command = job.verification[0].command if job.verification else "true"
        return AdapterCommand(
            framework=self.framework,
            command=["/bin/sh", "-lc", command],
            cwd=str(cwd),
            notes=[
                "Local adapter runs the first verification command as a deterministic smoke task."
            ],
        )


class ClaudeCodeAdapter(FrameworkAdapter):
    framework = Framework.CLAUDE_CODE
    executable = "claude"

    def build_command(self, job: AgentJob, context_pack: ContextPack, cwd: Path) -> AdapterCommand:
        prompt = context_pack.to_markdown()
        return AdapterCommand(
            framework=self.framework,
            command=["claude", "--print", prompt],
            cwd=str(cwd),
            notes=[
                "Uses Claude Code direct CLI when available.",
                "Use an installed runner plugin when isolated execution is required.",
            ],
        )


class CodexAdapter(FrameworkAdapter):
    framework = Framework.CODEX
    executable = "codex"

    def build_command(self, job: AgentJob, context_pack: ContextPack, cwd: Path) -> AdapterCommand:
        prompt = (
            f"Use /goal for this job if available.\n\n{context_pack.to_markdown()}"
        )
        return AdapterCommand(
            framework=self.framework,
            command=["codex", "exec", prompt],
            cwd=str(cwd),
            notes=[
                "Codex adapter is /goal-friendly by embedding a durable objective."
            ],
        )


class CursorAdapter(FrameworkAdapter):
    framework = Framework.CURSOR
    executable = "cursor"

    def build_command(self, job: AgentJob, context_pack: ContextPack, cwd: Path) -> AdapterCommand:
        return AdapterCommand(
            framework=self.framework,
            command=["cursor", str(cwd)],
            cwd=str(cwd),
            notes=[
                "Cursor is usually interactive; open the repo and use the generated context pack.",
                context_pack.to_markdown(),
            ],
        )


class RooCodeAdapter(FrameworkAdapter):
    framework = Framework.ROO_CODE
    executable = "roo"

    def build_command(self, job: AgentJob, context_pack: ContextPack, cwd: Path) -> AdapterCommand:
        return AdapterCommand(
            framework=self.framework,
            command=["roo", "run", "--stdin"],
            cwd=str(cwd),
            notes=[
                "Roo Code CLI names vary by installation.",
                "This command is the agent-ops contract and may require local alias setup.",
                context_pack.to_markdown(),
            ],
        )


class ClineAdapter(FrameworkAdapter):
    framework = Framework.CLINE
    executable = "cline"

    def build_command(self, job: AgentJob, context_pack: ContextPack, cwd: Path) -> AdapterCommand:
        return AdapterCommand(
            framework=self.framework,
            command=["cline", "run", "--stdin"],
            cwd=str(cwd),
            notes=[
                "Cline CLI names vary by installation.",
                "This command is the agent-ops contract and may require local alias setup.",
                context_pack.to_markdown(),
            ],
        )


class OpenClawAdapter(FrameworkAdapter):
    framework = Framework.OPENCLAW
    executable = "openclaw"

    def build_command(self, job: AgentJob, context_pack: ContextPack, cwd: Path) -> AdapterCommand:
        return AdapterCommand(
            framework=self.framework,
            command=["openclaw", "jobs", "submit", "--context-pack", f"{context_pack.id}.json"],
            cwd=str(cwd),
            notes=[
                "OpenClaw/clanker should manage jobs through context packs.",
                "Result handling should use normalized agent-ops manifests.",
            ],
        )


ADAPTERS: dict[Framework, FrameworkAdapter] = {
    Framework.LOCAL: LocalAdapter(),
    Framework.CLAUDE_CODE: ClaudeCodeAdapter(),
    Framework.CODEX: CodexAdapter(),
    Framework.CURSOR: CursorAdapter(),
    Framework.ROO_CODE: RooCodeAdapter(),
    Framework.CLINE: ClineAdapter(),
    Framework.OPENCLAW: OpenClawAdapter(),
}


def get_adapter(framework: Framework) -> FrameworkAdapter:
    if framework not in ADAPTERS:
        raise KeyError(framework.value)
    return ADAPTERS[framework]
