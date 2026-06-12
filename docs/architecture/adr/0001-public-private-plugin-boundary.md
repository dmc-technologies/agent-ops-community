# ADR 0001: Community Core And Extension Package Boundary

- **Status:** accepted
- **Date:** 2026-06-11
- **Deciders:** Dan Mueller, software architecture/security review
- **Related:** initial public community release

## Context

The project needs a public community repository while preserving a strict
boundary around runner-specific implementations and internal operational
workflows that are not appropriate for a general-purpose package.

Repository visibility and git history are repository-wide. Publishing from an
existing internal repository after deleting files can still expose prior
non-public content through history. Community users also need a working package
without access to organization-specific extensions.

## Decision

Create this repository as the clean community core. Keep runner-specific and
organization-specific behavior in extension packages that depend on this core
and register extensions through `agent_ops.plugins` entry points. The community
core must not import, reference, or depend on non-public extension code.

## Consequences

- **Positive:** community users get a working package, and extension packages can
  evolve without leaking implementation details.
- **Negative:** extensions must track public plugin contracts.
- **Neutral:** generic improvements should land here first; extension packages
  should stay thin.

## Alternatives Considered

1. Publish an existing internal repository after deleting non-public files. This
   lost because history can retain deleted content.
2. Maintain a separate copy manually. This lost because duplicated maintenance
   creates drift.
3. Make community code depend on organization-specific code. This lost because
   public installs would not work for community users.

## Rollback

If the split proves wrong, archive the public repository and continue internal
development elsewhere. Do not merge non-public implementation history into this
repository.
