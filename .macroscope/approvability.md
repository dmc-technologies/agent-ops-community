---
preview: false
neverApprove:
  - "AGENTS.md"
  - "DECISIONS.md"
  - "ARCHITECTURE.md"
  - "**/adr/**"
  - "**/decisions/**"
  - "docs/DECISIONS.md"
  - "docs/architecture/**"
  - "docs/developer/architecture/**"
  - "CODEOWNERS"
  - ".github/workflows/**"
  - "**/.github/workflows/**"
  - ".github/CODEOWNERS"
  - "**/CODEOWNERS"
  - ".env"
  - ".env.*"
  - "**/*secret*"
  - "**/*credentials*"
  - "deploy/**"
  - ".macroscope/ignore.md"
  - ".macroscope/check-run-agents/**"
---

Use Macroscope's default approvability criteria.

Additionally, any pull request that changes product defaults is not auto-approvable and requires human review.

Any pull request that adds or broadens a directive that disables or suppresses a lint,
type-checker, LSP, or other static-analysis diagnostic is not auto-approvable and requires
human review. This includes file-level, line-level, and configuration-level overrides.
This includes noqa, type: ignore, eslint-disable, and similar suppressions.

This file is live (preview: false) per CTO 2026-11-14 11:41 AM PT relaying Dan via Radar
("Go live, T3 style"), under t702u.
