from __future__ import annotations

from pathlib import Path

from agent_ops.registries.models import Framework

SUPPORTED_BOOTSTRAPS = [
    Framework.CODEX,
    Framework.CLAUDE_CODE,
    Framework.CURSOR,
    Framework.ROO_CODE,
    Framework.CLINE,
    Framework.OPENCLAW,
    Framework.LOCAL,
]

KNOWLEDGE_GIT_EXPORT = (
    'export KNOWLEDGE_GIT_URL="${KNOWLEDGE_GIT_URL:-'
    'git@github.com:your-org/agent-knowledge.git}"'
)


def bootstrap_text(framework: Framework) -> str:
    return f"""# Agent Ops Bootstrap: {framework.value}

Use Agent Ops Community as the portable public agent operations layer.

## Portable Environment

Set these variables for your machine:

```bash
export AGENT_OPS_HOME="${{AGENT_OPS_HOME:-$PWD}}"
export AGENT_KNOWLEDGE_HOME="${{AGENT_KNOWLEDGE_HOME:-~/agent-knowledge}}"
export CODEX_HOME="${{CODEX_HOME:-~/.codex}}"
export CLAUDE_HOME="${{CLAUDE_HOME:-~/.claude}}"
export CURSOR_HOME="${{CURSOR_HOME:-~/.cursor}}"
export ROO_CODE_HOME="${{ROO_CODE_HOME:-~/.roo-code}}"
export CLINE_HOME="${{CLINE_HOME:-~/.cline}}"
export OPENCLAW_HOME="${{OPENCLAW_HOME:-~/.openclaw}}"
export AGENT_OPS_LOCAL_HOME="${{AGENT_OPS_LOCAL_HOME:-~/.agentops}}"
export KNOWLEDGE_MEMORY_DIR="${{KNOWLEDGE_MEMORY_DIR:-$AGENT_KNOWLEDGE_HOME}}"
{KNOWLEDGE_GIT_EXPORT}
```

Install Agent Ops from wherever this repo is cloned:

```bash
cd "$AGENT_OPS_HOME"
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
export PATH="$AGENT_OPS_HOME/.venv/bin:$PATH"
```

## Agent Knowledge Setup

`agent-knowledge` is the durable shared memory plane. Use it before nontrivial
work when prior context may matter, and after important decisions or durable
discoveries that future agents should inherit.

Preferred path: configure the `agent-knowledge` MCP server for this framework
with:

```bash
KNOWLEDGE_MEMORY_DIR="$AGENT_KNOWLEDGE_HOME"
KNOWLEDGE_GIT_URL="$KNOWLEDGE_GIT_URL"
```

Fallback path when MCP is not available:

```bash
test -d "$AGENT_KNOWLEDGE_HOME/.git" || git clone "$KNOWLEDGE_GIT_URL" "$AGENT_KNOWLEDGE_HOME"
rg -n "<project|repo|workflow keyword>" "$AGENT_KNOWLEDGE_HOME"
```

## Required Operating Loop

1. Inspect capabilities, skills, tools, and frameworks:

   ```bash
   agentops capabilities list
   agentops skills list
   agentops tools list
   agentops frameworks list
   ```

2. Build a context pack for this framework:

   ```bash
   agentops context build <job.yaml> --framework {framework.value}
   ```

3. Use framework command handoff when useful:

   ```bash
   agentops frameworks command <job.yaml> --framework {framework.value} --json
   ```

4. Run installed plugin-backed execution paths when a runner plugin is present:

   ```bash
   agentops run <job.yaml> --dry-run --json
   ```

5. Verify before completion:

   ```bash
   agentops verify <job.yaml> --json
   ```

## Shared Skill/Tool Expectations

- Use `agent-knowledge` for durable memory; prefer MCP and use git/file
  fallback when MCP is unavailable.
- Use planning, testing, debugging, and verification skills when they match the
  work.
- Run repo CI checks when available; they are the project's independent trust
  boundary.
- Keep active handoff state in repo-local progress files or issue trackers.
"""


def write_bootstrap(framework: Framework, output_dir: Path) -> Path:
    target_dir = output_dir / framework.value
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "AGENTOPS.md"
    target.write_text(bootstrap_text(framework), encoding="utf-8")
    return target


def write_all_bootstraps(output_dir: Path) -> list[Path]:
    written = [write_bootstrap(framework, output_dir) for framework in SUPPORTED_BOOTSTRAPS]
    index = output_dir / "README.md"
    lines = [
        "# Agent Ops Bootstraps",
        "",
        "Generated framework handoff instructions:",
        "",
    ]
    lines.extend(f"- [{path.parent.name}]({path.parent.name}/AGENTOPS.md)" for path in written)
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [index, *written]
