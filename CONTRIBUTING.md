# Contributing

Keep contributions generic and reusable. This repository is the public core, so
runner-specific internals, private prompts, and organization-specific workflows
belong in plugins or private overlays.

Before opening a pull request, run:

```bash
ruff check .
pytest
```

