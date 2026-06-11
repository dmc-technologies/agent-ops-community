# ADR 0001: Public Core And Private Plugin Boundary

- **Status:** accepted
- **Date:** 2026-06-11
- **Deciders:** Dan Mueller, software architecture/security review
- **Related:** initial public community release

## Context

The project needs a public community repository while preserving a strict
boundary around proprietary runner implementations and internal operational
workflows.

Repository visibility and git history are repository-wide. Publishing from an
existing private repository after deleting files can still expose prior private
content through history. Community users also need a working package without
access to private code.

## Decision

Create this repository as the clean public core. Keep proprietary behavior in
private overlay packages that depend on this public core and register extensions
through `agent_ops.plugins` entry points. The public core must not import,
reference, or depend on private code.

## Consequences

- **Positive:** community users get a working package, and private extensions can
  evolve without leaking implementation details.
- **Negative:** private integrations must track public plugin contracts.
- **Neutral:** generic improvements should land here first; private packages
  should stay thin.

## Alternatives Considered

1. Publish an existing private repository after deleting private files. This lost
   because history can retain deleted content.
2. Maintain a separate copy manually. This lost because duplicated maintenance
   creates drift.
3. Make public code depend on private code. This lost because public installs
   would not work for community users.

## Rollback

If the split proves wrong, archive the public repository and continue internal
development privately. Do not merge private implementation history into this
repository.

