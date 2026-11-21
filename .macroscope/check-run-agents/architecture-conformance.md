---
title: Architecture Conformance
model: gpt-6-1-sol
reasoning: high
input: full_diff
tools:
- browse_code
include:
- '**/*'
exclude:
- .macroscope/**
- .review/**
- .coderabbit/pre-merge-checks/severity-gate.md
- .github/workflows/generated-file-guard.yml
- .review-selftest/marked/**
maxBudgetPerRun: 5
maxBudgetPerPR: 25
maxRuns: 2
conclusion: failure
showToolCalls: true
---
<!-- GENERATED from dmc-technologies/review-rules. Do not edit here: change review-rules (dmc-developers) and let the sync open a pull request. -->

# Check: Architecture-rule conformance

Purpose: automatic architecture checking. A conflict with an ADR or contract doc, a second
owner, a failed design check, or a change no rule covers means **Needs Human**. Route: CTO
first, then Dan's morning list as proposed rule text. This check never edits the pull request.

## Output contract (every run)
Every finding cites a governing rule as `<file>:<line>` read from the base branch, with the rule
quoted word for word; OR, when the file or the quoted rule isn't on base, as `<file>:<line> on <branch>`
read from the repo's active working branch named in .review/paths.json, with the rule quoted word
for word; OR says exactly `contract gap: <what the change adds that no rule covers>`.
A finding with none of these is invalid; do not post it. End every run with one line:
`Architecture result: PASS` or `Architecture result: CONFLICT <file>:<line>` (one per finding)
or `Architecture result: CONTRACT GAP`.

Report `contract gap` when the change adds a subsystem, store, boundary, owner, data format,
gate or review rule that no ADR, DECISIONS.md entry or contract doc covers, and the pull request
adds no decision for it. A contract gap is Needs Human, not a pass.

## 1. Follows the ADRs (ported verbatim from the CodeRabbit custom check)
Find the governing decision record for this repository. For the Momentum group (dmc-technologies/momentum, dmc-technologies/momentum-utilities, dmc-technologies/momentum-dev-ops, dmc-technologies/structures-utilities, dmc-technologies/momentum-documents, dmc-technologies/momentum-divergent, and dmc-technologies/momentum-community) it is docs/developer/architecture/adr/ and ARCHITECTURE.md in dmc-technologies/momentum: read them from the base branch of this pull request in dmc-technologies/momentum, and from the linked dmc-technologies/momentum repository in every other Momentum-group repository, together with that repository's own ARCHITECTURE.md and decision records when present. The other repositories use their own record: dmc-technologies/agent-ops: docs/DECISIONS.md, docs/decisions/, and ARCHITECTURE.md; dmc-technologies/agent-ops-community: docs/architecture/adr/ and ARCHITECTURE.md; dmc-technologies/agent-knowledge: decisions/; dmc-technologies/business-ops: docs/architecture/adr/ and ARCHITECTURE.md. Report Inconclusive, not Failed, when the governing record cannot be read. Fail when it holds no decision. Fail if the change does any of the following: stores or caches a recorded fact anywhere other than the store the record names; gives a recorded fact a second owner; defines an operation, tool type, or provider boundary in a way the record rules out; puts a product family or Design Element family, such as a tube, strut, truss, rod, or space-frame type, field, or step, into Momentum Core or into a module, route, or result field that the record or the pull request calls generic; adds a subsystem, store, boundary, or owner that the record does not cover and the pull request adds no decision; or edits a decision without stating in the description which decision changed and why. Name the decision and quote its rule in the finding. Pass only when no failure condition holds: the change is consistent with every decision, or it adds the decision that records the new choice. State which files you examined and what they established. In a repository not named above, report Passed and state that no governing record is configured for it. When this check fails, write each failure message in exactly the format below, with each part on its own line and in this order. The first line is an instruction for how the comment begins. Copy the last line character for character. In the Governing rule line, cite the rule by file path, heading, and line (ARCHITECTURE.md, DECISIONS.md, or an ADR), then read the cited file at review time and quote the cited rule word for word.
Begin the comment with the stop sign emoji followed by ARCHITECTURE CONFLICT (critical)
Governing rule (`<file>, <heading>, line <line>`): "<the cited rule, quoted word for word from the file>"
Layer: this change is in <layer A, for example Momentum Core or a generic route>; the behavior belongs in <layer B, for example the Design Element family module the record names>.
Owner: <owner path or role, for example the store or module the record names>
Fix: <concrete fix shape, for example move the recorded fact back to the store the record names and delete the second copy>

## 2. One owner, one store (ported verbatim)
This check covers the Momentum group: dmc-technologies/momentum, dmc-technologies/momentum-utilities, dmc-technologies/momentum-dev-ops, dmc-technologies/structures-utilities, dmc-technologies/momentum-documents, dmc-technologies/momentum-divergent, and dmc-technologies/momentum-community. Examine every changed file. Fail if the change keeps a recorded input, result, or decision in a second place; adds a cache, projection, or index that stores state instead of being rebuilt from the Git record store; or lets a provider, adapter, or external tool own a Design Element identity or introduce a second hardware lifecycle. Name the second copy or owner in the finding. Pass only when no changed file meets a failure condition. Decisions: Momentum ADR 0003, ADR 0007, ADR 0011. State which files you examined and what they established. In any other repository, report Passed and state that this check covers only the Momentum group.

## 3. A check says what it examined (ported verbatim)
This check covers the Momentum group: dmc-technologies/momentum, dmc-technologies/momentum-utilities, dmc-technologies/momentum-dev-ops, dmc-technologies/structures-utilities, dmc-technologies/momentum-documents, dmc-technologies/momentum-divergent, and dmc-technologies/momentum-community. Fail if an added or changed verification, check, or report in code outside tests/ can pass after examining nothing (an assertion only inside a loop over a collection that may be empty, a comparison of a value with itself, a return before the assertion), treats a missing result as success, or reports a result without stating which records or files it examined and what it could establish. Tests under tests/ and the pull request description belong to the warning-level sub-rule "A test or PR says what it examined" below, not to this error-level rule. A test or PR says what it examined, warning level: This check covers the Momentum group: dmc-technologies/momentum, dmc-technologies/momentum-utilities, dmc-technologies/momentum-dev-ops, dmc-technologies/structures-utilities, dmc-technologies/momentum-documents, dmc-technologies/momentum-divergent, and dmc-technologies/momentum-community. Fail if an added or changed test under tests/ can pass after examining nothing (an assertion only inside a loop over a collection that may be empty, a comparison of a value with itself, a return before the assertion), treats a missing result as success, or reports a result without stating which records or files it examined; or if the pull request description reports a result without stating which records or files it examined and what it could establish. Pass when every test and the description name what they covered. Decisions: Momentum ADR 0005, ADR 0006. State which files you examined and what they established. In any other repository, report Passed and state that this check covers only the Momentum group. This sub-rule warns and does not error: when it is the only failure, report it as a warning and do not fail this check. Pass when every check names what it covered and fails when that is absent. Decisions: Momentum ADR 0005, ADR 0006. State which files you examined and what they established. In any other repository, report Passed and state that this check covers only the Momentum group.

## 4. Design check (draft for CTO review)
When the pull request body links a design note, compare the change with it. Fail when the change
moves ownership, a data format or a gate to a different layer than the note names, or adds
behavior the note does not cover. Cite the note as `<file>:<line>`. When the change touches
engineering behavior or physics and links no joint SWE+Mech Eng design note with Mech Eng's
verdict, report `contract gap: no design note` (operating model, Dan t762u).

## 5. Idealizer owns model choices. Severity: CRITICAL
Scope: dmc-technologies/structures-utilities only; elsewhere report Passed and say so.
An error-level "yes" is a hard stop: `Architecture result: CONFLICT <file>:<line>`, Needs Human, routed to CTO. Begin each finding `CRITICAL: Idealizer owns model choices`.

Governing rules:
- momentum docs/developer/architecture/adr/0021-any-assembly-through-a-composed-approved-plan.md, "Slots", line 23: "assembly records in, analysis model out; one rule per Design Element family and per joint kind; a detail level per part; a request may override a rule" Read the governing file from the PR's base branch and quote it word for word. If the file or the quoted rule isn't on base, read it from the repo's active working branch listed in .review/paths.json, quote it word for word, and name the branch you quoted.
- same file, line 71: "The analysis model is the one object every idealizer writes and every solver adapter reads. It is a record, not a deck." Read the governing file from the PR's base branch and quote it word for word. If the file or the quoted rule isn't on base, read it from the repo's active working branch listed in .review/paths.json, quote it word for word, and name the branch you quoted.
- structures-utilities docs/DECISIONS.md, line 17: "5. The builder's analysis model object is the contract; a Nastran deck is one export of it" Read the governing file from the PR's base branch and quote it word for word. If the file or the quoted rule isn't on base, read it from the repo's active working branch listed in .review/paths.json, quote it word for word, and name the branch you quoted.
- momentum ARCHITECTURE.md, line 105 on demo/sprint-10: "The owner tables in this document say where code lives today; when they and a decision or ADR name different owners for one rule, the decision names the owner and every other module calls it." Read the governing file from the PR's base branch and quote it word for word. If the file or the quoted rule isn't on base, read it from the repo's active working branch listed in .review/paths.json, quote it word for word, and name the branch you quoted.

Model choices: element type and section idealization (beam/rod/shell/solid; PBAR vs PBEAM, offsets, shear factors, principal axes, I12, tube-to-section formula); RBE2/RBE3 (also CBUSH); joint/pin releases; fitting mass (also joint/fitting stiffness); CONM2 placement, CG, inertia frame (R*I*R^T about CG); SPCs and interface stiffness; detail level per part (mesh); units and WTMASS. How a recorded load is applied to nodes.
Not model choices: section size; analysis type, load cases, factors of safety, solver choice; recorded mass/CG/inertia passed through with source; material by catalog id (ADR 0021 line 42).

Error checks (any yes = CONFLICT):
1. Does a changed line in src/structures_mcp/tools/, routes.py, builders/ (not idealizer.py) or sizing/ expose a model choice as a tool/route option or parameter, or compute, decide or default one?
2. Does femap_nastran/deck.py or mystran/deck.py choose, compute, default or correct a model choice instead of writing the analysis_model.py field? (A mass-only CONM2 from the idealizer's fitting-mass field is serialization.)
3. Does sizing/frame_fe.py take payload mass/CG/inertia other than via interface_cg/interface_frame, or fitting mass other than from the idealizer's field?
4. Does any later layer (adapter, route, catalog, UI, report) overwrite an idealizer output?
Warning only: a CG computation, inertia rotation or interface frame outside interface_frame/interface_cg or frame_fe.

Do not flag: request fields that pass idealization overrides to idealizer.py (ADR 0021 line 48); payload_idealization and the layout-screening RBE2 path (DECISIONS.md lines 31-35) until a decision moves them.

Evidence per finding: head file:line, governing file:line quoted, owner, fix; list files examined.

FAIL: routes.py adds `rigid="rbe3"` as a route parameter. Check 1 is yes; the owner is idealizer.py.
PASS: idealizer.py adds a strut rule (CBAR with pinned-end releases), and deck.py writes those fields unchanged. Every check is no.

## Always Needs Human
- Any CONFLICT or CONTRACT GAP result.
- An Inconclusive result because the governing record could not be read.
- Any edit to a decision record that does not state which decision changed and why.
