# Decisions

Repository: `agent-ops-community`

Record durable architecture, workflow, and harness decisions here.

### 2026-08-26: Retire the shared Codex pull-request reviewer

- Decision: Installed product repositories use CodeRabbit's built-in automatic and incremental review flow. Agent Ops Community no longer publishes a label dispatcher, reusable AI review workflow, prompt, or review script.
- Rationale: CodeRabbit is the approved one and only AI review standard; retaining a second reviewer would preserve the architecture being replaced.
- Applies to: `.github/`, review documentation, tests, README, and repository handoff.
- Revisit when: Dan approves a new review architecture through a separate design.

### 2026-08-19: Managed deployment status classifies from the installed ownership manifest

- Decision: `DeploymentEngine.status` reads each managed target's installed ownership manifest through the shared cooperative status-evidence reader that preview already used, and passes that manifest plus a manifest-versus-installed audit to `DeploymentRegistry.status`. The receipt bound to the current registry snapshot supplies only the last resolved commit and the recorded failure or missing-ref outcome. Managed evidence requires no preview review state and a full 40-hex source revision; preview evidence keeps its `unreviewed-local` state and 64-hex fingerprint. Native Windows uses the same contract through a shared-lock manifest read in the Windows backend.
- Rationale: passing `manifest=None` for managed targets forced `TargetState.STALE` for every managed target regardless of its installed state, so `status` could never agree with `refresh` or `audit` and the stable, branch, and modified classifications were unreachable. The installed manifest is the same artifact `audit` validates, so reading it removes the contradiction without a fetch or a receipt write.
- Applies to: `agent_ops.deployment.engine.status`, the shared status-evidence reader in `agent_ops.deployment.transaction`, the native Windows transaction backend, deployment documentation, and deployment engine, registry, and Windows tests.
- Revisit when: managed status needs the current channel-ref commit rather than the last resolved commit, which would require a fetch and a different command contract.

### 2026-08-18: Integration branch roles determine repository progress ownership

- Decision: when repository instructions declare an integration branch, routine feature pull requests hand off through exact pull-request and tracker evidence without replacing repository progress. The integration controller and stable-branch landing own bounded progress updates. Repositories without an integration branch retain the existing single-branch closeout.
- Rationale: concurrent feature pull requests otherwise overwrite one shared handoff file, while forcing full stable-branch acceptance on each feature slows early delivery. Branch-role ownership preserves one durable controller state without imposing repository-specific branch names or CI tiers.
- Applies to: generated `AGENTS.md` and `.agentops/harness/BOOTSTRAP.md`, Community harness tests, and repository-specific integration-delivery adapters.
- Revisit when: Agent Ops gains a typed repository delivery profile that can validate branch roles directly.

### 2026-08-17: Prime gstack legacy ownership moves only through the shared transaction

- Decision: permit one Prime-only typed transition from `.agentops/gstack-prime-manifest.json` into the shared public-skill ownership manifest. Declare it only for the public Prime gstack plan when no shared manifest exists. Under the existing target lock, require the canonical public gstack repository, exact legacy schema, owner, pinned ref, canonical complete planned path set, current file fingerprints, single-link regular files, and planned modes. Before any replacement, retain no-follow descriptor authority over every accepted file and the retired manifest through backup-backed operations, including accepted files already equal to the plan. Persist exact operation, path, fingerprint, mode, and filesystem identity before each move. If the moved backup differs because the path changed after classification, journal that safe backup as concurrent evidence before no-replace restoration. Roll back earlier operations, never overwrite another live entry, and abort before shared ownership publication. Treat the legacy manifest and its files as rollback-backed prior state; never prepublish a synthetic shared manifest.
- Rationale: earlier Prime installs legitimately own current files whose newly rendered bytes can differ even at the same pinned upstream ref. The legacy manifest is sufficient migration evidence only when it is bound to the installed provider and current complete plan and remains recoverable inside the transaction.
- Applies to: Prime gstack public planning, shared transaction apply, rollback, recovery, audit, Prime documentation, and deployment tests.
- Revisit when: all supported Prime homes have a shared ownership manifest and the legacy transition can be removed.

### 2026-08-17: Self-approval rejection does not replace a successful Review Gate result

- Decision: when the model result passes, treat GitHub's exact `Unprocessable Entity (HTTP 422)` response to the optional approving review as nonfatal only if trusted API reads resolve both the authenticated actor and pull-request author and their login identities match. Keep every other approval publication error, failed identity read, and actor mismatch fail-closed.
- Rationale: GitHub forbids an author from approving their own pull request. The review comment, exact-head status, and passing model result remain valid evidence; an optional approval cannot be created by that actor and must not convert those facts into a false gate failure.
- Applies to: `.github/scripts/review_gate.py`, `tests/test_review_gate.py`, and repositories using the shared Review Gate workflow.
- Revisit when: GitHub exposes a stable machine-readable error code for self-approval or the shared workflow separates the pull-request author from the approval identity.

- Correction: treat the exact GitHub CLI `Unprocessable Entity (HTTP 422)` approval response as the bounded nonfatal condition without assuming the token identity matches the pull-request author. A live shared-workflow retry proved GitHub can return the same response when those identities differ. All other approval publication errors remain fail-closed.
- Rationale: the optional approval is not the Review Gate result. The already-published model comment and exact-head status carry that result, while GitHub can reject approval because of actor identity or repository token policy without exposing a more specific response through the CLI.

### 2026-08-16: Channel launch is limited to frameworks with a proven isolated-home contract

- Decision: retain no-follow descriptors for every audited ownership manifest, managed file, and managed directory until receipt publication or launch handoff, and revalidate the retained identity, mode, topology, and bytes immediately before terminal success. Channel launch is enabled only for Local and Codex, whose isolated-home readiness contracts are implemented and tested; it rejects Claude Code, Cursor, OpenCode, Prime Agent, and OpenClaw until their native isolated-home contracts are proven.
- Rationale: a cooperative target lock alone cannot stop an external writer from changing an audited target, and the unsupported framework adapters previously supplied setup commands that could not establish authoritative readiness. Refusal prevents false success and avoids placing runtime state outside the selected target.
- Applies to: deployment transaction evidence, engine terminal operations, preview, channel launch, framework adapters, and their tests.
- Revisit when: each excluded framework has an exact native home selector, precedence-clearing environment, and readiness probe verified against its real runtime.

### 2026-08-15: Branch channels are machine-local data selections over one shared transaction

- Decision: keep executable deployment authority in the reviewed installed community package. Treat stable and branch repositories as provider-declared data sources, give every target an isolated framework home, keep registry, source store, locks, and receipts machine-local, and use one grouped descriptor-bound transaction, audit, and reverse recovery path for every provider and target.
- Rationale: one implementation prevents provider-specific installation drift, while independent machine state permits rapid branch deployment without making branch content executable or coupling machines through locks or receipts.
- Applies to: deployment models, provider discovery, source store, registry, transaction engine, orchestration, CLI, launch adapters, documentation, and acceptance tests.
- Revisit when: a supported framework cannot isolate native state by home, or a provider needs executable source behavior that cannot remain in the reviewed package.

### 2026-08-15: Local preview is selected unreviewed data and cannot become a managed channel

- Decision: preview requires an explicit authored checkout, explicit skill selection, and an existing preview-reserved target. Installed providers must bind every requested name exactly once to a canonical skill identity and exclusively owned paths; omit providers that own none of the selection. Permit only the exact internal read-only Git metadata command allowlist with executable configuration disabled. Retain descriptor authority for the selected paths and parents plus Git index and HEAD through provider planning; after target locks, repeat planning and retain shared authority for the exact original registry snapshot through terminal success. Revalidate the target channel, home, framework, registry bytes and identity, source path, mode, byte, tracked state, and commit before installation and after install and audit. Treat any terminal mismatch as a primary failure inside the same reverse recovery boundary, retaining both failures and transaction evidence when recovery is incomplete. Fingerprint the selected closure as SHA-256, mark the result `unreviewed-local`, and apply it through the shared transaction without fetching, publishing, building, importing, or writing a managed source snapshot. Persist the exact target channel in every strict ownership manifest. Preview status takes the established target lock cooperatively and retains descriptor authority for the manifest and every owned path through terminal identity, mode, and content validation without managed fetch or receipts. Reject managed plan, refresh, audit, deploy, switch, and launch treatment of preview targets.

- Rationale: authors can try local skill edits quickly while the target remains isolated and visibly outside stable or branch review state. The content fingerprint and transaction evidence preserve exact local facts without granting the working tree code authority.
- Applies to: local preview, managed engine selection, deployment CLI, documentation, and tests.
- Revisit when: a reviewed promotion workflow can bind an unreviewed preview fingerprint to an immutable committed source without weakening the current boundary.

### 2026-08-15: Every transaction authorizes one exact channel pair

- Decision: default grouped installs, refresh, and preview require an existing prior ownership manifest to use the current planned target channel. Switch and deploy must instead supply the original registry target channel and candidate plan channel. Persist and validate both values through grouped apply, rollback, and recovery; reject any other prior manifest, candidate manifest, or transaction-record combination before mutation.
- Rationale: a manifest cannot grant itself authority to move a target between stable and branch channels; only the registry-backed engine can authorize that exact transition.
- Applies to: grouped install, managed refresh, preview, switch, deploy, rollback, recovery, transaction evidence, documentation, and tests.
- Revisit when: a reviewed promotion operation introduces another registry-authorized channel transition.
### 2026-08-16: Only model results may be reused as Review Gate resolution state

- Decision: a preflight result never creates reusable resolution state. Legacy state comments without recorded provenance are reusable only when their trusted Review Gate comment records `Backend: codex`; new state records Codex provenance in its signed payload. Keep preflight comments separate when posting the later model result.
- Rationale: deterministic checks can change without a pull-request commit. Replaying an old preflight finding prevents the corrected check and model from running, while replaying an actual same-head Codex result avoids unnecessary repeat discovery.
- Applies to: `.github/scripts/review_gate.py` and `tests/test_review_gate.py`.
- Revisit when: Review Gate gains a versioned persistent result store that can retain backend provenance without relying on GitHub comment structure.

- Correction: retain the requested model-comment body while scanning existing comments. Scanned comments are evidence only; they must never replace the body passed to the POST or PATCH request.
- Rationale: replacement with a preflight comment would discard the signed Codex state and let a later same-head retry rediscover instead of carrying a known blocker.

### 2026-08-13: HumanLayer show-me stays an exact pinned dependency

- Decision: install only `plugins/show-me/skills/show-me` from `humanlayer/skills` version 1.0.0 at commit `4d8d644ca747517973f58d7953f58d7cd07520cd` through a fingerprint-owned transactional adapter for all six managed agent hosts. Keep the upstream skill identity and core instructions, but replace its host-specific HTML opener with a portable artifact-preview fallback.
- Rationale: one exact upstream commit makes harness installation reproducible while preserving the requested skill identity, excluding the repository's four unrelated skills, refusing user-owned collisions, and keeping the HTML artifact path usable across agent hosts.
- Applies to: the source and packaged skill-dependency registries, framework skill installation, and Prime Agent setup documentation.
- Revisit when: HumanLayer publishes a required correction, the skill gains executable resources, or a supported framework needs a host-specific adaptation.

### 2026-08-09: Prime Agent uses native handoff and namespaced owned bundles

- Decision: register Prime Agent as `prime-agent`; require every framework command handoff to receive an existing directory through `--cwd`; hand context packs to `prime-agent --print --cwd <repo> -- <prompt>` using that same directory; default installs to `${PRIME_AGENT_CODING_AGENT_DIR:-$HOME/.prime/agent}`; and generate namespaced `agentops-gstack-*` and `agentops-superpowers-*` skills with fingerprint ownership manifests outside the shared skill namespace.
- Rationale: Prime Agent 0.7.1 discovers coding-agent resources through `PRIME_AGENT_CODING_AGENT_DIR`, while the upstream pinned bundles contain provider-specific paths and tool contracts. Explicit Prime generation plus collision preflight preserves frequently used workflows without overwriting a person’s skills or exposing raw Claude instructions. A validated repository directory also prevents the reported adapter working directory from diverging from Prime Agent’s `--cwd`.
- Applies to: framework and registry models, adapters, bootstrap generation, Prime bundle adapters, skill dependency installation, public data registries, documentation, and tests.
- Revisit when: Prime Agent changes its CLI or skill discovery contract, or either upstream project publishes and tests an equivalent Prime host package.

### 2026-08-08: AI review runs once on the final head with risk-routed Sol effort

- Decision: a person applies `ai review` once when a pull request's final head is ready. A low-effort `gpt-5.6-sol` classifier evaluates the complete diff and routes the full review to the same model at low, medium, high, or xhigh effort. Low confidence and critical authority domains route to xhigh. Auto-labeling and fast advisory mode are removed.
- Rationale: historical review data showed repeated full reviews concentrated cost and comments on a few difficult pull requests. Final-head triggering removes repeated full reviews, while risk routing preserves xhigh for credentials, authorization, engineering source authority, persistent evidence, and similarly difficult changes regardless of diff size.
- Applies to: `.github/workflows/review-gate.yml`, `.github/workflows/review-gate-reusable.yml`, `.github/scripts/review_gate.py`, and `tests/test_review_gate.py`.
- Revisit when: production profile summaries provide enough low, medium, high, and xhigh outcomes to compare material-finding recall and run cost by tier.

### 2026-08-08: Review Gate blocks only current material implementation defects

- Decision: a Codex observation blocks only when the pull request introduced an incorrect current behavior, a plausible current path reaches it, the consequence is material, evidence and confidence are high, the issue is not already reported by deterministic checks, and a correction is required before merge. Missing tests, possible future regressions, style, general hardening, and approval prerequisites do not block. Repeated instances are grouped by root cause.
- Rationale: the historical sample contained many requests for coverage, documentation, hardening, and prerequisites without a demonstrated current material consequence. Exact-head shadow evaluation removed one such test-only blocker while retaining a current false-verification defect.
- Applies to: `.github/review-gate-prompt.md`, `.github/scripts/review_gate.py`, `tests/test_review_gate.py`, and `docs/review-gate-evaluation.md`.
- Revisit when: shadow or production review shows a material defect was suppressed, or non-material observations still create review cycles.

### 2026-08-03: Repositories own their CI tier vocabulary

- Decision: newly generated harnesses use `Harness Check` and `CI Contract`
  sections. The checker also accepts a complete legacy `Fast Gate` and `Full
  Gate` pair so released harnesses remain valid after upgrade. Each repository
  defines its named local tiers and hosted routing inside `CI Contract`.
- Rationale: verification cost and deployment boundaries differ by project.
  The shared harness should ensure the contract is visible without imposing
  terminology that conflicts with the repository's executable CI interface.
- Applies to: `src/agent_ops/harness.py`, generated
  `.agentops/harness/VERIFY.md` files, and `tests/test_harness.py`.
- Revisit when: Agent Ops gains a typed, machine-readable CI-tier schema shared
  by multiple repositories.

### 2026-06-19: AI review is label-driven, Codex-backed, and PR-backed

- Decision: PRs can run Codex-backed AI review by adding the `ai review` label. The workflow runs deterministic preflight checks first, invokes Codex with `.github/review-gate-prompt.md` against the PR diff, posts the `Review Gate` status, writes a durable summary comment plus per-finding comments, and attempts an approving PR review on pass.
- Rationale: Labels, commit statuses, PR comments, and PR reviews are visible GitHub primitives that stay CLI-verifiable without adding a daemon or separate review service.
- Applies to: `.github/workflows/review-gate.yml`, `.github/scripts/review_gate.py`, `tests/test_review_gate.py`.
- Revisit when: GitHub token policy blocks approval submission or Agent Ops adopts a stronger shared review backend.

### 2026-06-19: AI review uses trusted gate code and proposal-aligned principles

- Decision: Label-driven AI review loads the gate script from the default branch, reruns when an already-labeled PR receives new commits, and passes a harder public-safe prompt into Codex covering architecture, AI, mechanical/domain, product, security, evidence, and deployment posture.
- Rationale: Review automation should not execute PR-controlled gate code with write permissions, approvals must not go stale after new commits, and the review lens should enforce source-grounded engineering evidence without naming private proposal details.
- Applies to: `.github/workflows/review-gate.yml`, `.github/review-gate-prompt.md`, `tests/test_review_gate.py`.
- Revisit when the review backend can enforce these principles with richer inline review APIs or repository-specific policy packs.

### 2026-08-10: Prime excludes workflows whose provider and runtime contracts cannot be preserved

- Decision: The Prime gstack adapter excludes model benchmarking, paired external agents, automated multi-model planning, multi-model office hours, the provider-specific headed browser, automated shipping, and browser-skill authoring. It also rejects custom gstack profile paths that are unsafe as unquoted shell words. Superpowers resolves the selected profile once and rejects symlinked skills or ownership-metadata write paths.
- Rationale: Exclusion is the smallest safe correction when text substitution would mislabel an external provider or a retained workflow would require omitted or provider-specific runtime behavior. Failing before writes preserves profile confinement and prevents commands from splitting an absolute path.
- Applies to: Prime gstack generation, Superpowers installation, focused adapter tests, and Prime support documentation.
- Revisit when: a workflow has an explicit Prime-native provider operation and complete packaged runtime, or generated gstack commands gain context-aware path serialization.

### 2026-08-13: Adapted show-me installation follows each host's active profile and filesystem capabilities

- Decision: HumanLayer `show-me` installs through a dedicated ownership transaction. POSIX hosts use no-follow directory descriptors and `/dev/fd`; Windows uses reparse-point checks, `msvcrt` locking, same-volume atomic renames, and the same fingerprint recovery rules. Collision discovery recursively checks nested host-visible skill definitions, OpenCode root Markdown definitions, and every documented global compatibility or configured root for Codex, Cursor, OpenCode, and OpenClaw; it refuses linked definitions it cannot confine and permits an alternate-root copy only when its exact bytes match the adapted installation. OpenClaw profile selection follows `OPENCLAW_STATE_DIR`, then the parent of `OPENCLAW_CONFIG_PATH`, then a validated `OPENCLAW_PROFILE`, then normalized `OPENCLAW_HOME`, `HOME`, `USERPROFILE`, Termux, and native account-home precedence. Legacy `.clawdbot` state discovery does not relocate the managed config-directory skill root.
- Rationale: The managed installer must neither overwrite user-authored skills nor write into a host's inactive profile, and its safety contract must work on macOS and Windows rather than depending on Linux procfs or locking APIs.
- Applies to: `src/agent_ops/show_me_adapter.py`, `src/agent_ops/skill_installer.py`, generated bootstraps, and skill-installer tests.
- Revisit when: a supported host changes global skill discovery, OpenClaw changes state/config precedence, or Python exposes a stronger Windows directory-relative filesystem API.

## Template

### 2026-08-17: Exact unchanged managed files retain filesystem identity

- Decision: when an existing managed regular file has the exact bytes and mode required by the next plan, record it through the established non-mutating transaction operation instead of staging, backing up, and replacing it. Continue to replace any file whose bytes or mode change. Runtime Python caches remain outside managed source ownership and are accepted only by the existing source-bound bytecode validator; refresh does not delete an unrelated or invalid cache.
- Rationale: replacing an unchanged Python source changes its modification time and inode, which invalidates a cache created by normal import even though the source bytes did not change. Reusing the existing non-mutating transaction path preserves the source/cache relationship without expanding deletion authority or changing rollback and recovery formats.
- Applies to: shared provider-plan install, refresh, rollback, recovery, runtime-cache audit, and transaction tests.
- Revisit when: a provider requires generated output whose validity depends on metadata beyond exact source bytes and mode, or the transaction schema gains a separately named preserved-file operation.

### 2026-08-17: Retired managed Python sources own a finite derived-cache cleanup matrix

- Decision: when a plan removes an exact Python source from the prior ownership manifest, the locked transaction checks only that source's CPython 3.11–3.14 PEP 3147 cache paths for default, `opt-1`, and `opt-2` modes. The running interpreter tag retains full code-object equality validation. Another listed tag requires its known magic, a timestamp header bound to the exact source modification time and byte length, exact derived name and parent, regular single-link `0644` identity, and a bounded file size. Each accepted cache uses the existing provenance, backup, rollback, recovery, and tamper-evidence operation. Invalid and unrelated files remain present and audit-visible.
- Rationale: an agent update may retire source installed by another released supported CPython minor, but arbitrary cache names or foreign files must never gain deletion authority. A finite source-derived namespace closes the stale discovery path without installing another runtime or interpreting foreign marshal payloads.
- Applies to: shared provider-plan source retirement, runtime-cache transaction evidence, rollback, recovery, audit, documentation, and transaction tests.
- Revisit when: the repository deliberately adds or removes a released CPython minor from cache-retirement support, CPython changes the cache-name or timestamp-header contract, or Agent Ops adopts a stronger cross-version bytecode verifier.

### 2026-08-17: Source-store state stays private and audit receipts retain their first result

- Decision: require the source-store root, sources, source directory, snapshot reads, and lock directory to be owned by the effective user with exact `0700` mode. Persist an HTTPS remote without userinfo and pass that userinfo only as transient Git fetch authentication. Retain the first audit result through receipt creation and refuse any later result that differs, including a previously missing managed file.
- Rationale: local source mirrors can contain private repository data, and their configuration must not retain reusable credentials. A receipt represents the audit that authorized it, not a later changed target state.
- Applies to: `src/agent_ops/deployment/source_store.py`, deployment audit retention, and their regression tests.
- Revisit when: the source-store adds an approved credential helper or a durable encrypted credential boundary.

- Correction: Git receives transient HTTPS Basic authorization through its per-process `GIT_CONFIG_*` environment rather than a `-c` command argument. Missing audited directories are represented by the expected missing file set while every present directory remains descriptor-pinned.
- Rationale: process arguments can be read by another local account; an absent directory cannot be opened as evidence, but repeated complete audit equality still prevents a changed result from receiving a receipt.

### 2026-08-19: Audit tolerates an opted-in bytecode cache from any supported CPython

- Decision: the audit unexpected-file scan resolves a `__pycache__` candidate's source from the PEP 3147 name for any supported CPython 3.11–3.14 tag with default, `opt-1`, or `opt-2` naming, requires that source to be declared in the plan's runtime Python sources, and accepts the file only through the shared runtime-cache provenance check. The running interpreter's tag keeps exact compiled code-object equality; another supported tag requires known magic, a timestamp and source-length header bound to the live source, exact derived name and parent, regular single-link `0644` identity, and bounded size. Nothing else changes: unrelated files, invalid caches, caches for sources the plan does not declare, and any managed file whose bytes or mode differ still fail audit.
- Rationale: resolving only the running interpreter's cache tag classified a cache left by a previously installed CPython minor as an unexpected unmanaged file. That permanently blocked the affected target: audit reported `modified` and every refresh installed correctly, failed its post-install audit, and rolled back, so the target could never reach the current channel commit. Retirement already trusts the same finite matrix and the same foreign-tag evidence, so audit now uses one shared acceptance rule instead of a narrower one.
- Applies to: `audit_provider_plans` in `src/agent_ops/deployment/transaction.py`, grouped refresh acceptance, `README.md`, `ARCHITECTURE.md`, and the deployment transaction and engine tests.
- Revisit when: the repository adds or removes a supported CPython minor, CPython changes the cache-name or timestamp-header contract, Agent Ops adopts a cross-version bytecode verifier, or refresh gains authority to delete a stale cache whose source it still owns.

### YYYY-MM-DD: Decision title

- Decision: what changed or what standard was chosen.
- Rationale: why this is the right tradeoff.
- Applies to: files, commands, workflows, or repositories affected.
- Revisit when: condition that should trigger review or removal.
