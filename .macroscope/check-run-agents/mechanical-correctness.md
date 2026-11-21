---
title: Mechanical Engineering Correctness
model: gpt-6-1-sol
reasoning: medium
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
maxBudgetPerRun: 2
maxBudgetPerPR: 10
conclusion: failure
showToolCalls: true
---
<!-- GENERATED from dmc-technologies/review-rules. Do not edit here: change review-rules (dmc-developers) and let the sync open a pull request. -->

# Check: Mechanical engineering correctness (owner of content: Mech Eng)

Purpose: route the PR. This check only labels it PHYSICS or NO PHYSICS by section 1; it does not judge the physics and gives no physics findings. Mech Eng's sign-off at the exact head SHA is the human gate (record form agreed with SWE: the mech-eng-review check run from App 5764640, bound to the head SHA). A "doesn't smell right" verdict blocks the PR and routes to CTO.

## 1. What counts as physics

### Paths (either repo)
- structures-utilities: `src/structures_mcp/sizing/**`, `builders/idealizer.py`, `builders/payload_adapter.py`, `tools/femap_nastran/**`, `tools/frame_fe/**`, `**/hardware_check*`, criteria/factors/loads files, `docs/**/requests/**`
- momentum: `demos/*/criteria.json`, `**/program_criteria.py`, any sizing, margin, hardware-check, mass or weight module
- Both: unit modules and constants; material property records; mass, fitting, rod-end and end-hardware rules; optimizer objectives, constraints, tiers and separation limits (Peregrine/OpenMDAO setup); deck writers (SOL, PARAM, SPC, MPC/RBE, EIGRL, CONM2, MAT, PROD/PBAR cards); f06/pch/op2 parsers; frequency requirement constants; Rhino/Grasshopper geometry-to-idealization; demo templates that print margins, frequencies or masses; `tests/**/goldens/**` and fixtures holding expected physics values.
- Excluded: prototype folders (no PRs for prototypes, per Dan).

### Diff kinds (anywhere, even outside the paths above)
Flag any added or changed line that:
- has a numeric literal next to a unit or physics name (`lb`, `lbf`, `psi`, `ksi`, `in`, `Hz`, `g`, `kg`, `N`, `Pa`, `mm`, `m`, `slinch`, `E`, `rho`, `nu`, `Ftu`, `Fty`, `Fcy`, `FS`, `factor`, `margin`, `MS`, `mass`, `load`, `freq`, `stiffness`)
- touches a conversion constant (`386.0886`, `0.0025900790`, `LB_PER_KG`, `KG_PER_SLINCH`, `G_IN_S2`, `WTMASS`)
- changes a citation, source label or factor label string
- changes a pass/fail comparison on a margin or frequency (`>=`, `>`, `abs(`, tolerance values)
- changes a boundary condition, constraint DOF set, element type or rigid element
- changes an expected value in a test or golden


<!-- Section 7 is appended by review-rules (ENG-97; MODEV-1230 cut sections 2-6). Section 1 above is Mech Eng's text, unchanged. -->

## 7. Check title (ENG-97, CTO 9:01 PM PT 2026-11-15, wording under t1117u)
The mech-eng-review workflow reads this check's title and nothing else, so set the check title to
one of exactly two values:
- exactly `NO PHYSICS` only when none of section 1 applies to any added, removed or changed line
  or file (no physics path and no diff kind);
- exactly `PHYSICS: NEEDS MECH ENG` in every other case, including when you are unsure.
Add no other words to the title. Put the section 1 matches and the files you examined in the summary.
`NO PHYSICS` lets the pull request pass without Mech Eng; `PHYSICS: NEEDS MECH ENG` holds it for
Mech Eng's sign-off at the head SHA.
