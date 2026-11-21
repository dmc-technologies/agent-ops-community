---
preview: false
neverApprove:
- '**/CODEOWNERS'
- '**/rulesets/**'
- settings/rulesets*
- '**/branch-protection*'
- .github/workflows/**
- '**/.github/workflows/**'
- .github/agent-accounts
- .github/dependabot.yml
- catalog/skills/pr-review/scripts/**
- '**/.env'
- '**/.env.*'
- '**/*secret*'
- '**/*credential*'
- '**/*.pem'
- '**/*.key'
- deploy/**
- '**/deploy/**'
- '**/*deploy*.sh'
- infra/**
- '**/terraform/**'
- '**/AGENTS.md'
- '**/DECISIONS.md'
- '**/ARCHITECTURE.md'
- '**/adr/**'
- '**/decisions/**'
- configs/global/review-policy.md
- configs/global/AGENTS.md
- .macroscope/**
- .review/**
- .coderabbit.yaml
- .coderabbit.config.ts
- rules/**
- overrides/**
- sync/**
- targets.yml
- src/structures_mcp/sizing/**
- src/structures_mcp/builders/idealizer.py
- src/structures_mcp/builders/payload_adapter.py
- src/structures_mcp/builders/rod.py
- src/structures_mcp/builders/ground_structure.py
- src/structures_mcp/builders/design_space.py
- src/structures_mcp/tools/femap_nastran/**
- src/structures_mcp/tools/frame_fe/**
- src/structures_mcp/tools/mystran/**
- src/structures_mcp/tools/peregrine_rhino/**
- src/structures_mcp/tools/eso/**
- src/structures_mcp/tools/salome_code_aster/**
- src/structures_mcp/tools/prepomax_calculix/**
- src/structures_mcp/solve/**
- src/structures_mcp/analysis_modes/**
- src/structures_mcp/catalogs/**
- src/structures_mcp/column_buckling.py
- docs/**/requests/**
- inputs/layout-screening/**
- inputs/recipe-evaluation/**
- src/structures_mcp/capabilities.py
- demos/*/criteria.json
- demos/*/data/**
- '**/program_criteria.py'
- momentum/engineering/**
- momentum/integrations/nastran/**
- momentum/integrations/member_sizing_provider.py
- momentum/integrations/engineering_data.py
- momentum/knowledge/hand_calc_adapter.py
- momentum/knowledge/data/equation_map/**
- momentum/framework/models/materials.py
- momentum/framework/material_supplier.py
- momentum/candidate_packages/material_suppliers.py
- momentum/candidate_packages/catalogs/**
- outputs/reference-demos/**
- momentum/framework/core_records.py
- momentum/plan/loop_stage.py
- momentum/plan/loop_observation.py
- momentum/integrations/screen_result.py
- momentum/plan/stage_fills.py
- demos/*/request.md
- demos/*/problem.json
- momentum/operations/verbs/design.py
- momentum/interfaces/desktop_text.py
- momentum/interfaces/assembly_report.py
- '**/hardware_check*'
- '**/*units*.py'
- '**/*criteria*'
- '**/*factors*'
- '**/*loads*'
- '**/*sizing*'
- '**/*margin*'
- '**/openmdao/**'
- '**/peregrine*/**'
- '**/goldens/**'
- '**/layout-space-frame.md'
- '**/*frequency*'
- '**/*screening_profile*'
- '**/*physics*'
- '**/*physics*/**'
- '**/*hand_check*'
- '**/*hand_check*/**'
- '**/*hand_calc*'
- '**/*hand_calc*/**'
- '**/*idealiz*'
- '**/*idealiz*/**'
- '**/*solver*'
- '**/*solver*/**'
- '**/*nastran*'
- '**/*nastran*/**'
- '**/*buckling*'
- '**/*buckling*/**'
- '**/*stiffness*'
- '**/*stiffness*/**'
- '**/*material*'
- '**/*material*/**'
- '**/*joint*'
- '**/*joint*/**'
- '**/*fitting*'
- '**/*fitting*/**'
- '**/*catalog*'
- '**/*catalog*/**'
- '**/*.bdf'
- '**/*.nas'
- '**/*.dat'
- '**/*.f06'
- '**/*.inp'
- '**/*.fem'
---
<!-- GENERATED from dmc-technologies/review-rules. Do not edit here: change review-rules (dmc-developers) and let the sync open a pull request. -->

Use Macroscope's default approvability criteria.

DMC Needs Human rules. A pull request is not auto-approvable, and requires human review, when any
of the following holds:

1. Critical infrastructure: any changed file matches neverApprove, or the Critical Infrastructure
   check reports a finding or did not complete. The human is a dmc-developers member.
2. Mechanical engineering correctness: the Mechanical Engineering Correctness check reports any
   finding or did not complete, or the diff is physics-bearing under that check's section 1
   (physics path or content trigger). The human is Mech Eng's sign-off at the head SHA, with Dan
   for anything Mech Eng flags.
3. Architecture conformance: the Architecture Conformance check reports a CONFLICT or a CONTRACT
   GAP, is Inconclusive, or did not complete. Route to CTO, then Dan.
4. It adds or broadens a directive that disables or suppresses a lint, type-checker, LSP or other
   static-analysis diagnostic, at file, line or configuration level.
5. It changes product defaults.
6. It carries the label `judgment-stop` (rules/bounded-tries.md, Judgment stop). A dmc-developers
   member decides the next step; never auto-approve it.
