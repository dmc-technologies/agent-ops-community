# Plugin Authoring

Agent Ops Community supports runner plugins through the `agent_ops.plugins`
entry-point group.

## Minimal Runner Plugin

```python
from __future__ import annotations

from dataclasses import dataclass

from agent_ops.contracts.job import AgentJob
from agent_ops.contracts.result import RunResult, RunStatus
from agent_ops.plugins import AgentOpsPlugin


@dataclass(frozen=True)
class ExampleRunner:
    name: str = "example"

    def supports(self, job: AgentJob) -> bool:
        return job.runner == self.name

    def run(self, job: AgentJob, *, dry_run: bool = False) -> RunResult:
        return RunResult(
            job_id=job.id,
            runner=self.name,
            mode=job.mode.value,
            status=RunStatus.DRY_RUN if dry_run else RunStatus.PASS,
        )


def plugin() -> AgentOpsPlugin:
    return AgentOpsPlugin(runners=(ExampleRunner(),))
```

Register it from another package:

```toml
[project.entry-points."agent_ops.plugins"]
example = "example_agent_ops_plugin:plugin"
```

