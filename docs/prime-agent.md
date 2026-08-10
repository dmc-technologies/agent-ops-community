# Prime Agent Support

Prime Agent is a first-class Agent Ops framework. Agent Ops builds Prime Agent context packs, produces direct CLI handoffs, generates bootstrap instructions, and installs collision-safe Prime variants of the pinned gstack and Superpowers bundles.

## Bootstrap and skills

Generate the Prime Agent bootstrap and install both configured bundles:

```bash
agentops bootstrap prime-agent
agentops skills install prime-agent
```

The default Prime Agent profile is `${PRIME_AGENT_CODING_AGENT_DIR:-$HOME/.prime/agent}`. Use `--home` to select another profile and `--dependency` to install only `gstack` or `superpowers`. Restart an existing Prime Agent session after installing or updating skills so it reloads the profile inventory.

Prime gstack installation runs the pinned upstream external-host generator with a Prime host definition, builds its runtime through the pinned Bun lockfile, and installs 38 generated skills with `agentops-gstack-*` directory and frontmatter names. Browser, design, and PDF runtime assets live under `.agentops/runtime/gstack`; they currently use about 600 MB on Linux. Provider-specific, self-management, and hook-enforced safety workflows that do not have a correct Prime contract are excluded. The generated instructions use IPython for project shell and file work, RLM plus `agent_message` for child agents, ordinary assistant responses for user questions, and `/skill:agentops-gstack-*` for internal skill selection.

Prime Superpowers installation adapts all 14 pinned upstream skills with `agentops-superpowers-*` directory and frontmatter names. It rewrites internal skill references and supplies a Prime startup and tool contract. Higher-priority system, project, and user policy continues to outrank every adapted skill.

## Collision and upgrade contract

The adapters preflight every managed target before writing. A path can be updated or removed only when its current fingerprint matches the prior Agent Ops ownership manifest. An unowned path, a modified managed path, an unsafe manifest entry, or another skill declaring the same namespaced frontmatter name stops installation without changing that content. Writes are staged and rolled back if a transaction fails.

The first safe install can migrate the earlier raw Agent Ops copies. Migration occurs only when `skills/gstack` or the old flat Superpowers directories match the exact pinned upstream source, including directory structure and executable behavior where relevant. Any local change causes a refusal instead of deletion.

The dependency registry pins both upstream repositories to exact Git commits. gstack generation runs `bun install --frozen-lockfile`, and installation stops before profile writes if Bun, generation, or required runtime compilation fails.

## Context handoff

Build or inspect the command without starting Prime Agent:

```bash
agentops context build <job.yaml> --framework prime-agent
agentops frameworks command <job.yaml> --framework prime-agent --cwd <repo> --json
```

The caller must provide the existing job repository through `--cwd`. The handoff follows the Prime Agent CLI contract:

```bash
prime-agent --print --cwd <repo> -- <context-pack-markdown>
```

`--print` requests one non-interactive response, `--cwd` selects the job repository, and `--` prevents context text from being parsed as command-line options. The adapter reports `prime-agent` availability from the executable on `PATH`.
