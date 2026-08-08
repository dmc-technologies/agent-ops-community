# Review Gate risk-routing evaluation

## Purpose

This evaluation checks whether one final-head Review Gate can retain material defect detection while reducing repeated reviews and non-material comments. It uses public pull-request history and exact historical commits. The sample is intentionally weighted toward difficult changes; it is a recall check, not a forecast of the eventual production effort distribution.

## Historical review audit

- 71 completed Codex finding runs across 21 pull requests produced 112 findings.
- Three difficult pull requests accounted for 44 review rounds and 74 findings.
- A single final-head review per pull request would have reduced expensive full reviews from 71 to at most 21 in this sample before any reasoning-effort savings.
- A manual 38-finding sample classified 16 as material defects, 11 as deterministic failures or prerequisites, and 11 as hardening, documentation, or test requests without a demonstrated current material consequence.

## Classifier shadow evaluation

The low-effort Sol classifier reviewed 11 exact historical heads. Four contained CI, test-infrastructure, or operational changes routed to `high`. Seven changes involving credentials, authorization, engineering source authority, persistent evidence, or execution contracts routed to `xhigh`. Small diffs involving credentials, concurrency, and approval evidence still routed to `xhigh`, confirming that changed-line count did not lower review fidelity.

No case in this risk-heavy sample routed to `low` or `medium`. Production summaries expose the selected effort, classifier confidence, and risk domains so a broader shadow sample can measure those tiers before changing the routing rules.

## Materiality shadow evaluation

Two exact historical heads exercised opposite outcomes with the new materiality contract:

- A prior blocker that requested deterministic regression tests for otherwise correct lock-retry behavior was suppressed after the contract required an incorrect current behavior rather than missing coverage or possible future regression. The rerun passed with zero blockers in 564.8 seconds at `xhigh`.
- A difficult CI and engineering-contract change still failed. The reviewer traced a changed default tier to two active handoff commands that could claim full verification after running only the light subset. The result produced one root-cause blocker in 1,031.9 seconds at `xhigh`.

These cases establish the minimum acceptance result: remove a demonstrated test-only review cycle while retaining a demonstrated current false-verification path. They do not establish an exact monetary reduction because the evaluation login did not expose per-run billing telemetry.

## Production controls

- A person applies `ai review` once when the final head is ready; open, reopen, and synchronize events do not run the gate.
- Auto-labeling and fast advisory mode are removed.
- The classifier always uses `gpt-5.6-sol` at low effort. The full review always uses the same model at the selected `low`, `medium`, `high`, or `xhigh` effort.
- Low classifier confidence and critical authority domains route to `xhigh`.
- The full review covers the complete base-to-head diff and reports only high-confidence, PR-introduced current implementation defects with a material consequence.
- Findings already reported by deterministic checks are suppressed, and repeated instances of one root cause become one comment.
- The workflow has a 30-minute total timeout.
