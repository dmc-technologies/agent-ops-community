# Prime Agent Support

Prime Agent is a first-class Agent Ops framework. Agent Ops can build Prime Agent context packs, produce a direct CLI handoff, generate bootstrap instructions, and install the pinned gstack and Superpowers skill bundles.

## Bootstrap and skills

Generate the Prime Agent bootstrap and install both configured bundles:

```bash
agentops bootstrap prime-agent
agentops skills install prime-agent
```

The default Prime Agent home is `${PRIME_AGENT_CODING_AGENT_DIR:-~/.prime/agent}`. gstack is installed as a complete repository bundle at `skills/gstack`; every skill from the pinned Superpowers `skills/` tree is merged into `skills/`. Use `--home` to target a different Prime Agent home and `--dependency` to install only one configured bundle.

The dependency registry pins both repositories to exact Git commits. Agent Ops checks out those commits before copying files, so repeated installs use the same source until the registry is deliberately updated.

## Context handoff

Build or inspect the command without starting Prime Agent:

```bash
agentops context build <job.yaml> --framework prime-agent
agentops frameworks command <job.yaml> --framework prime-agent --cwd <repo> --json
```

The handoff follows the Prime Agent CLI contract:

```bash
prime-agent --print --cwd <repo> -- <context-pack-markdown>
```

`--print` requests one non-interactive response, `--cwd` selects the job repository, and `--` prevents context text from being parsed as command-line options. The adapter reports `prime-agent` availability from the executable on `PATH`.
