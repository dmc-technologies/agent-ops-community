# Update one shared collection of skills and policy

You can install an exact collection once, inspect its source, and restore the preceding collection without fetching it again. Skills and optional global policy activate together. A collection fingerprint identifies those choices; each source retains its original Git commit.

This command surface activates shared content. It does not register a harness's discovery paths or migrate an existing skill directory. Machine onboarding must establish those paths and ownership separately. Fresh sessions use the selected content; running sessions are not promised automatic reload.

## Inspect and operate

Use an initialized deployment registry with schema version 2 and a `shared_selection`. Existing schema version 1 registries retain their previous behavior. The registry selects exact source commits and relative skill directories, and may select one policy file from a selected source.

```sh
agentops machine install --config machine.yaml --json
agentops machine status --config machine.yaml --json
agentops machine sync --config machine.yaml --json
agentops machine rollback --config machine.yaml --json
```

Pass `--state-home PATH` to keep source caches in an explicit location. `status` reads installed evidence without fetching. Missing or modified content returns a nonzero exit. `rollback` restores the recorded preceding collection from verified retained files; it refuses modified or missing recovery content.

Find where a skill came from before editing it:

```sh
agentops machine source --config machine.yaml --skill example --json
agentops machine source --config machine.yaml --skill example --checkout ./source-repository --json
agentops machine updates --config machine.yaml --json
```

The source result names the repository, exact commit, and relative directory. An optional checkout must match that source; its current commit and local modifications are reported. Installed snapshot directories are generated content and cannot serve as authoring checkouts. Upstream inspection reports `current`, `available`, or `unknown`; it never rewrites the selection or activates an update. An unavailable source is unknown, not evidence of freshness.

## Initialize through the existing registry API

The embedding distribution creates its machine registry through `DeploymentRegistry.save`, which establishes the ownership and locking evidence. Hand-writing YAML without that initialization does not establish ownership and is refused.

```python
from pathlib import Path
from agent_ops.deployment.models import SourceSpec
from agent_ops.deployment.registry import (
    DeploymentRegistry, RegistryConfig, SharedSelectionConfig,
)
from agent_ops.deployment.shared_selection import SharedSelection

source_commit = "EXACT_40_CHARACTER_GIT_COMMIT"
selection = SharedSelection(sources=({
    "source_id": "team",
    "commit": source_commit,
    "skill_paths": ["skills/example"],
},))
registry = DeploymentRegistry(Path("machine.yaml").absolute())
registry.save(RegistryConfig(
    schema_version=2,
    sources=(SourceSpec("team", "https://example.com/team/skills.git"),),
    channels=(),
    targets=(),
    shared_selection=SharedSelectionConfig(
        "shared-skills", Path.home() / ".agentops/shared", selection,
    ),
))
```

Replace the example source, commit, and skill path with actual values. Policy can be included as `policy={"source_id": "team", "path": "configs/global/AGENTS.md"}` in the selection. It uses the same source commit and becomes `policy/AGENTS.md` inside the snapshot; it is not treated as a skill.

## Publication and recovery

The engine stages verified files under `snapshots/<fingerprint>` and publishes the fixed `current` selector through its existing transaction journal. It retains preceding snapshot files, checks concurrent ownership changes, and recovers from process exits. Status checks file content, selector state, and source mapping; a directory's existence is insufficient.

Selector replacement is atomic on supported macOS and Linux filesystems. A reader that opens several files across an update can still cross generations; readers requiring one consistent generation must resolve and retain the snapshot directory once. Shared activation refuses on Windows before writing; existing Windows deployment operations remain separate.

Repository CI proves engineering behavior and remains required independently of CodeRabbit's review. The final `CI` job accounts for every required platform job. CodeRabbit inherits central review settings with additive deployment guidance; a person retains merge authority.
