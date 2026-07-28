# Review Gate: fast advisory mode

The Review Gate has two modes, selected per invocation of the reusable workflow
`.github/workflows/review-gate-reusable.yml`.

## Thorough mode (default) — the merge gate

`mode: thorough` (the default) is unchanged: it reviews the complete
`base...HEAD` diff, posts the required `Review Gate` status, submits the
approving review when it passes, and is the **only** mode with merge authority.

## Fast mode — non-blocking advisory

`mode: fast` is a fast, advisory pass to speed iteration. It is **not** the merge
gate:

- It posts a distinct, **non-required** status context, `Review Gate (fast
  advisory)`, and a separate advisory comment — it never touches the `Review
  Gate` status or the thorough gate's comments.
- It surfaces only **P0/P1** findings and **never submits an approving review**.
- It **always exits 0** so it can never read as a merge blocker.
- On rounds after the first it **delta re-reviews** only the commits since the
  last successfully-reviewed head and re-verifies carried findings, instead of
  re-reviewing the whole diff.

Fast mode is a development aid. The thorough `Review Gate` plus one required
green run on the final head remain required to merge.

## Invocation contract

Call the reusable workflow from a caller job. Fast mode runs **alongside** the
thorough job (e.g. gated on an `ai review fast` label), never in place of it:

```yaml
jobs:
  fast-advisory:
    if: contains(github.event.pull_request.labels.*.name, 'ai review fast')
    uses: <org>/agent-ops-community/.github/workflows/review-gate-reusable.yml@main
    with:
      pr_number: ${{ github.event.pull_request.number }}
      repo: ${{ github.repository }}
      head_repo: ${{ github.event.pull_request.head.repo.full_name }}
      head_sha: ${{ github.event.pull_request.head.sha }}
      base_ref: ${{ github.event.pull_request.base.ref }}
      mode: fast
      fast_codex_model: ${{ vars.REVIEW_GATE_FAST_CODEX_MODEL || '' }}
    secrets: inherit
```

### Ordering constraint (important)

A caller can only pass `mode`/`fast_codex_model` once the reusable workflow that
declares those inputs is on the ref the caller pins (`@main`). GitHub validates
reusable-workflow inputs against the pinned ref at startup, so wiring a caller to
`@main` before the inputs land on `main` causes a `startup_failure`. Add caller
wiring **after** the reusable changes merge to `main`.

## Optional inputs and secrets

| Name | Kind | Purpose |
| --- | --- | --- |
| `mode` | input | `thorough` (default) or `fast`. |
| `fast_codex_model` | input | Cheaper/faster Codex model for fast mode only. |
| `REVIEW_GATE_FAST_CODEX_MODEL` | repo var | Default fast-mode model. |
| `REVIEW_GATE_STATE_KEY` | secret | HMAC key that signs fast-mode delta state. |
| `REVIEW_GATE_BOT_LOGIN` | env | Expected advisory-comment author login. |

### Delta-state trust

Fast mode's delta checkpoint (which commits were already reviewed, and the
carried re-verification list) is stored in the advisory comment as an
**HMAC-signed** token. Without `REVIEW_GATE_STATE_KEY` configured, no state is
trusted and **every fast round does a full review** — the fail-closed default.
With a key set, the state cannot be forged or altered by any other actor, even
one sharing the gate's comment-author identity.
