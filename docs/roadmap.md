# Community Roadmap

Agent Ops Community should provide the full generic Agent Ops experience for
users who do not install organization-specific runner or verifier extensions.

## Already Present

- Repository harness templates and checks.
- Generic job/result contracts.
- Verification command execution.
- Runner plugin interface.
- Basic CLI surface.
- Public documentation, ADR, example job, and CI.
- Public-safety test for non-public terms and local paths.

## Next Generic Scope

These capabilities should move into the community package because they are
framework- and user-agnostic:

- Context-pack builder and models.
- Framework bootstrap generation for common agent frameworks.
- Framework command handoff adapters.
- Capability, skill, and tool registries.
- Catalog loader, compiler, and validation.
- Environment doctor profiles and checks.
- Public skill bundle installation.
- Richer examples that demonstrate the same workflow shape across supported
  agent frameworks.

## Extension-Only Scope

These capabilities should remain outside the community package and be supplied
by separately installed extensions:

- Runner-specific execution internals.
- Proprietary verifier prompts.
- Organization-owned job examples.
- Internal operational workflows.
- Logs, session artifacts, and private deployment conventions.
