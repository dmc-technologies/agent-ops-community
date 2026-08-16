# Review Gate Policy

Review once. Block only proven critical defects. File verified noncritical defects in one follow-up issue. Recheck only the critical fixes.

A person or authorized agent requests review after ordinary CI passes on the intended final head. The review is bound to that exact commit.

## Scope

- Lite review covers changed functions, direct callers, and affected tests.
- Critical review covers the complete diff and affected trust, data, source-authority, public-contract, and operational boundaries.
- Changed paths select the scope without a separate model call. The `critical` pull-request label or manual workflow input forces critical scope. Unknown changed paths fail closed to critical scope.

Sensitive changes receive deeper review but do not block by category alone.

## Results

A finding blocks only when the changed code has a current failure path, high-confidence evidence, and a concrete consequence in security, safety, data loss, broken core behavior, or false acceptance evidence. A disputed blocker receives focused adjudication, not another discovery review.

Verified noncritical defects do not fail the gate. Review Gate groups them into one issue linked to the pull request. Style preferences, hypothetical risks, missing coverage without a current defect, pre-existing problems, and failures already caught by CI are ignored.

## Fix checks

When a critical finding causes a correction, the replacement head gets a targeted resolution check. That check covers the prior blockers, the fix delta, directly affected callers or interfaces, and any new critical defect caused by the fix. It does not search unchanged code for new findings.

Each passing status remains exact-head evidence. A behavior-changing commit replaces the accepted head and requires the appropriate targeted check or a new discovery review when trusted prior state is unavailable.
