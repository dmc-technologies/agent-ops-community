# Community Roadmap

Agent Ops Community should provide the full generic Agent Ops experience for
users who do not install proprietary runner or verifier extensions. A community
user should get the same operational feel across common agent frameworks without
needing access to organization-owned packages.

## Already Present

- Repository harness templates and checks.
- Generic job/result contracts.
- Verification command execution.
- Runner plugin interface.
- Basic CLI surface.
- Public documentation, ADR, example job, and CI.
- Public-safety test for non-public terms and local paths.

## Next Generic Scope

These capabilities should move into the community package because they are part
of the general Agent Ops product experience:

- Context-pack builder and models.
- Framework bootstrap generation for common agent frameworks.
- Framework command handoff adapters for common agent frameworks.
- Capability, skill, and tool registries.
- Catalog loader, compiler, and validation.
- Environment doctor profiles and checks.
- Public skill bundle installation.
- Built-in execution adapters for non-proprietary agent frameworks where a
  stable public CLI or command contract exists.
- Richer examples that demonstrate the same workflow shape across supported
  agent frameworks.

## Extension-Only Scope

Only these capabilities should remain outside the community package and be
supplied by separately installed extensions:

- Proprietary runner implementations.
- Proprietary verifier prompts.
- Organization-owned job examples.
- Internal operational workflows.
- Logs, session artifacts, and private deployment conventions.
