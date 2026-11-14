---
preview: true
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

This file is inert until Dan approves a Macroscope install. preview: true must stay on;
CTO will flip it when Dan answers go-live. Do not install the app.
