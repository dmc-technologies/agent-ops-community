#!/usr/bin/env python3
"""Codex-backed PR review gate for label-triggered AI review workflows."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

COMMENT_MARKER = "<!-- review-gate-agent-review -->"
FINDING_MARKER_PREFIX = "<!-- review-gate-finding:"
# Fast mode is a NON-MERGE-AUTHORITY advisory lane. It uses its own comment
# marker and status context so it can never touch, overwrite, or approve the
# thorough gate's merge-authority surface (the COMMENT_MARKER summary, the
# per-finding FINDING_MARKER comments, the "Review Gate" status, and the
# approving PR review).
FAST_COMMENT_MARKER = "<!-- review-gate-fast-advisory -->"
# Machine-trusted delta state (checkpoint SHA + carry-forward titles) is stored as
# an HMAC-SIGNED token so it cannot be forged or altered by any other actor that
# shares the gate's comment-author identity (the shared Actions bot, a PAT owner's
# other comments). Without a signing key the gate stores no state and every fast
# round does a full review -- the fail-closed default. A human-readable copy is
# rendered separately for display only and is never trusted for state.
STATE_MARKER_PREFIX = "<!-- review-gate-fast-state:"
STATUS_CONTEXT_THOROUGH = "Review Gate"
STATUS_CONTEXT_FAST = "Review Gate (fast advisory)"
FAST_ADVISORY_SEVERITIES = {"P0", "P1"}
# Backend-failure finding codes: the review did not produce a valid disposition,
# so fast mode must not advance its delta checkpoint on any of these.
REVIEW_FAILURE_CODES = {
    "CODEX_REVIEW_FAILED",
    "CODEX_REVIEW_UNPARSEABLE",
    "CODEX_REVIEW_INVALID",
}
DEFAULT_REVIEW_PROMPT = """# Review Gate Prompt

Review this PR for necessity, company-policy alignment, architecture, AI safety,
security, domain correctness, and whether the change is strictly functional to
merge. Report only concrete, actionable findings with evidence.
"""
SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{20,}"
)


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    title: str
    detail: str
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewResult:
    backend: str
    summary: str = ""
    warnings: tuple[Finding, ...] = ()
    blocking: tuple[Finding, ...] = ()
    raw_review: str = ""

    @property
    def passed(self) -> bool:
        return not self.blocking


def run_command(
    args: list[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def bounded_tail(text: str, limit: int = 6000) -> str:
    if len(text) <= limit:
        return text
    return f"[truncated to last {limit} characters]\n{text[-limit:]}"


def codex_failure_detail(result: subprocess.CompletedProcess[str], raw_review: str) -> str:
    sections = [f"Codex CLI exited with status {result.returncode}."]
    if result.stderr.strip():
        sections.extend(["", "stderr tail:", bounded_tail(result.stderr.strip())])
    if result.stdout.strip():
        sections.extend(["", "stdout tail:", bounded_tail(result.stdout.strip())])
    if raw_review.strip() and raw_review != result.stdout:
        sections.extend(["", "last-message output tail:", bounded_tail(raw_review.strip())])
    return "\n".join(sections)


def codex_child_env() -> dict[str, str]:
    allowed = {
        "CODEX_HOME",
        "HOME",
        "PATH",
        "REVIEW_GATE_CODEX_MODEL",
        "REVIEW_GATE_FAST_CODEX_MODEL",
        "TEMP",
        "TMPDIR",
        "USER",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def resolve_codex_model(mode: str = "thorough") -> str:
    """Fast mode may use a cheaper/faster tier; falls back to the thorough model."""
    if mode == "fast":
        fast = os.environ.get("REVIEW_GATE_FAST_CODEX_MODEL", "").strip()
        if fast:
            return fast
    return os.environ.get("REVIEW_GATE_CODEX_MODEL", "").strip()


def codex_model_args(mode: str = "thorough") -> list[str]:
    model = resolve_codex_model(mode)
    if not model:
        return []
    return ["--model", model]


def iter_text_files(workspace: Path, changed_files: list[str] | None = None) -> list[Path]:
    skipped = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
    files: list[Path] = []
    if changed_files is not None:
        for rel in changed_files:
            path = workspace / rel
            if path.is_file() and path.stat().st_size <= 500_000:
                files.append(path)
        return files
    for path in workspace.rglob("*"):
        if any(part in skipped for part in path.parts):
            continue
        if path.is_file() and path.stat().st_size <= 500_000:
            files.append(path)
    return files


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""


def changed_files_for_pr(repo: str, pr_number: int) -> list[str] | None:
    override = os.environ.get("REVIEW_GATE_CHANGED_FILES", "")
    if override.strip():
        return [line.strip() for line in override.splitlines() if line.strip()]
    result = run_command(["gh", "pr", "diff", str(pr_number), "--repo", repo, "--name-only"])
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def has_conflict_markers(text: str) -> bool:
    markers = set()
    for line in text.splitlines():
        if line.startswith("<<<<<<< "):
            markers.add("start")
        elif line == "=======":
            markers.add("middle")
        elif line.startswith(">>>>>>> "):
            markers.add("end")
    return markers == {"start", "middle", "end"}


def analyze_workspace(workspace: Path, changed_files: list[str] | None = None) -> ReviewResult:
    blocking: list[Finding] = []
    warnings: list[Finding] = []
    conflict_files: list[str] = []
    secret_files: list[str] = []
    workflow_files: list[str] = []

    for path in iter_text_files(workspace, changed_files):
        rel = str(path.relative_to(workspace))
        text = read_text(path)
        if not text:
            continue
        if has_conflict_markers(text):
            conflict_files.append(rel)
        if SECRET_RE.search(text) and ".env.example" not in rel:
            secret_files.append(rel)
        if rel.startswith(".github/workflows/") and "pull_request_target" in text:
            workflow_files.append(rel)

    if conflict_files:
        blocking.append(
            Finding(
                "blocking",
                "MERGE_CONFLICT_MARKERS",
                "Merge conflict markers are present",
                "Resolve conflict markers before review can pass.",
                tuple(sorted(conflict_files)),
            )
        )
    if secret_files:
        blocking.append(
            Finding(
                "blocking",
                "POSSIBLE_SECRET",
                "Possible committed credential material",
                "Remove hardcoded secret-like values or move them to GitHub secrets.",
                tuple(sorted(secret_files)),
            )
        )
    if workflow_files:
        warnings.append(
            Finding(
                "warning",
                "PULL_REQUEST_TARGET",
                "Workflow uses pull_request_target",
                "Confirm the workflow does not check out or execute untrusted PR code.",
                tuple(sorted(workflow_files)),
            )
        )
    return ReviewResult("preflight", warnings=tuple(warnings), blocking=tuple(blocking))


def build_carried_section(carried_findings: tuple[str, ...]) -> str:
    """Delta rounds separately require explicit revalidation of prior findings.

    The diff SCOPE for a delta round is set once (the single `git diff` line in
    the prompt); this section does not restate a scope, only the carry-forward
    contract, so the prompt has exactly one authoritative diff scope.
    """
    if not carried_findings:
        return ""
    lines = [
        "## Carried findings — revalidate each explicitly",
        "",
        "Prior fast rounds reported the findings below. For EACH one, re-check it",
        "against the current head and state whether it still applies; include it as",
        "a finding if it is still present, even if it lies outside the delta above.",
    ]
    for title in carried_findings:
        lines.append(f"- {title}")
    return "\n".join(lines) + "\n\n"


def build_mode_section(mode: str) -> str:
    if mode == "fast":
        return (
            "## Fast advisory mode\n\n"
            "This is a fast, ADVISORY pass to give the author a quick directional\n"
            "signal during iteration. It is not the merge gate. Report only the\n"
            "highest-severity, clearly-blocking issues (P0/P1): correctness bugs,\n"
            "broken contracts, security regressions, and fabricated or untraceable\n"
            "engineering values. Skip P2/P3 stylistic or hardening notes in this\n"
            "pass -- the thorough gate covers those. Prefer a few high-confidence\n"
            "findings over exhaustive breadth.\n\n"
        )
    return ""


def build_codex_prompt(
    review_prompt: str,
    *,
    repo: str,
    pr_number: int,
    sha: str,
    base_ref: str,
    base_diff_ref: str,
    mode: str = "thorough",
    delta_from_sha: str | None = None,
    carried_findings: tuple[str, ...] = (),
) -> str:
    # Thorough mode always reviews the complete base...HEAD diff. Delta focus is
    # a fast-mode-only optimization; the thorough merge gate never narrows scope.
    if mode != "fast":
        delta_from_sha = None
        carried_findings = ()
    # Exactly one authoritative diff scope: the delta base on a fast delta round,
    # otherwise the full base...HEAD. The prompt never names a second scope.
    diff_scope_ref = delta_from_sha or base_diff_ref
    scope_note = ""
    if delta_from_sha:
        scope_note = (
            f"\nThis is a delta re-review: prior fast rounds already reviewed up to "
            f"{delta_from_sha}, so the diff above is exactly the new work since then "
            f"and is your authoritative review scope for new issues.\n"
        )
    mode_and_delta = build_mode_section(mode) + build_carried_section(carried_findings)
    return f"""You are executing the repository PR review gate.

Repository: {repo}
Pull request: #{pr_number}
Head SHA: {sha}
Base ref: {base_ref}

Use the local checkout as the source of truth. Review the PR diff with:

git diff {diff_scope_ref}...HEAD
{scope_note}
Apply this review prompt:

{review_prompt}

{mode_and_delta}## Exhaustiveness (single-pass completeness)

Enumerate EVERY blocking finding present in the diff scope shown above in this
one pass. Do not defer, hold, or ration findings for a later review round --
assume there is no cheap later round and that omitted issues ship. Before
returning:
- Re-scan the changes under review for additional INSTANCES of every issue class
  you found. If a defect appears in one place, check whether the same class
  appears elsewhere in that scope and report all instances.
- Group findings by root-cause class so one architectural fix can retire a whole
  class rather than surfacing one instance at a time across rounds.
Completeness in this pass is more important than brevity.

Return only valid JSON with this exact shape:
{{
  "verdict": "approve" | "comment" | "request_changes",
  "summary": "One concise paragraph on need, policy alignment, and merge readiness.",
  "findings": [
    {{
      "severity": "P0" | "P1" | "P2" | "P3",
      "title": "Short imperative finding title",
      "body": "Specific evidence, risk, and required fix.",
      "files": ["relative/path.ext"]
    }}
  ]
}}

Use P0/P1/P2 for findings that should block merge. Use P3 only for non-blocking advisory comments.
If there are no actionable findings, return "approve" with an empty findings array.
Do not include markdown fences or prose outside the JSON object.
"""


def ensure_base_ref(workspace: Path, repo: str, base_ref: str) -> str:
    target_ref = f"refs/remotes/review-gate-base/{base_ref}"
    run_command(["gh", "auth", "setup-git"], cwd=workspace)
    result = run_command(
        [
            "git",
            "fetch",
            "--force",
            "--depth=100",
            f"https://github.com/{repo}.git",
            f"{base_ref}:{target_ref}",
        ],
        cwd=workspace,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return target_ref


def extract_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Codex did not return a JSON object")
    data = json.loads(stripped[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("Codex JSON response must be an object")
    return data


def _valid_finding_entry(item: object) -> bool:
    """A finding must be an object with correctly-typed fields (all optional)."""
    if not isinstance(item, dict):
        return False
    for field in ("severity", "title", "body", "detail"):
        value = item.get(field)
        if value is not None and not isinstance(value, str):
            return False
    files = item.get("files")
    return files is None or isinstance(files, list)


def finding_from_payload(payload: dict, index: int) -> Finding:
    severity = str(payload.get("severity") or "P2").upper()
    if severity not in {"P0", "P1", "P2", "P3"}:
        severity = "P2"
    title = str(payload.get("title") or f"Codex finding {index + 1}").strip()
    detail = str(payload.get("body") or payload.get("detail") or "").strip()
    files_payload = payload.get("files") or []
    files = tuple(str(item) for item in files_payload if str(item).strip())
    return Finding(severity, f"CODEX_{severity}_{index + 1}", title, detail, files)


def run_codex_review(
    workspace: Path,
    review_prompt: str,
    *,
    repo: str,
    pr_number: int,
    sha: str,
    base_ref: str,
    mode: str = "thorough",
    delta_from_sha: str | None = None,
    carried_findings: tuple[str, ...] = (),
) -> ReviewResult:
    base_diff_ref = ensure_base_ref(workspace, repo, base_ref)
    output_path = workspace / ".review-gate-codex-output.json"
    prompt = build_codex_prompt(
        review_prompt,
        repo=repo,
        pr_number=pr_number,
        sha=sha,
        base_ref=base_ref,
        base_diff_ref=base_diff_ref,
        mode=mode,
        delta_from_sha=delta_from_sha,
        carried_findings=carried_findings,
    )
    result = run_command(
        [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-rules",
            "--sandbox",
            "danger-full-access",
            "--output-last-message",
            str(output_path),
            *codex_model_args(mode),
        ],
        cwd=workspace,
        env=codex_child_env(),
        input_text=prompt,
    )
    raw_review = output_path.read_text(encoding="utf-8") if output_path.exists() else result.stdout
    output_path.unlink(missing_ok=True)
    if result.returncode != 0:
        return ReviewResult(
            "codex",
            summary="Codex review failed to execute.",
            blocking=(
                Finding(
                    "P1",
                    "CODEX_REVIEW_FAILED",
                    "Codex review did not complete",
                    codex_failure_detail(result, raw_review),
                ),
            ),
            raw_review=raw_review,
        )

    try:
        payload = extract_json_object(raw_review)
    except (ValueError, json.JSONDecodeError) as exc:
        return ReviewResult(
            "codex",
            summary="Codex review returned an unparseable response.",
            blocking=(
                Finding(
                    "P1",
                    "CODEX_REVIEW_UNPARSEABLE",
                    "Codex review output was not valid structured JSON",
                    f"{exc}\n\nRaw output:\n{raw_review[:4000]}",
                ),
            ),
            raw_review=raw_review,
        )

    # Validate the full output contract before trusting the result. A payload
    # with an unknown verdict or a non-list findings field must NOT be treated as
    # a clean review: otherwise it can produce zero blockers (a silent pass) and,
    # in fast mode, advance the delta checkpoint past work that got no valid
    # disposition. Such payloads are a review failure, same as unparseable output.
    verdict_raw = payload.get("verdict")
    findings_payload = payload.get("findings")
    valid_verdict = (
        isinstance(verdict_raw, str)
        and verdict_raw.strip().lower() in {"approve", "comment", "request_changes"}
    )
    # Every findings entry must be a well-formed object. A non-object entry (e.g.
    # `[null]`) or a wrong-typed field must NOT be silently dropped: dropping it
    # reduces the blocking count and can turn request_changes into a false PASS
    # that submits an approving review. Any malformed entry is a contract failure.
    findings_well_formed = isinstance(findings_payload, list) and all(
        _valid_finding_entry(item) for item in findings_payload
    )
    if not valid_verdict or not findings_well_formed:
        findings_type = type(findings_payload).__name__
        return ReviewResult(
            "codex",
            summary="Codex review violated the required output contract.",
            blocking=(
                Finding(
                    "P1",
                    "CODEX_REVIEW_INVALID",
                    "Codex review output did not satisfy the required schema",
                    "Expected verdict in {approve, comment, request_changes} and a "
                    "list of well-formed finding objects (each an object with "
                    "string severity/title/body and a list `files`). Got "
                    f"verdict={verdict_raw!r}, findings type={findings_type}.",
                ),
            ),
            raw_review=raw_review,
        )
    verdict = verdict_raw.strip().lower()
    summary = str(payload.get("summary") or "").strip()
    findings = tuple(
        finding_from_payload(item, index) for index, item in enumerate(findings_payload)
    )
    blocking = tuple(
        finding
        for finding in findings
        if verdict == "request_changes" or finding.severity in {"P0", "P1", "P2"}
    )
    warnings = tuple(finding for finding in findings if finding not in blocking)
    return ReviewResult(
        "codex",
        summary=summary,
        warnings=warnings,
        blocking=blocking,
        raw_review=raw_review,
    )


def render_section(title: str, findings: tuple[Finding, ...], empty: str) -> list[str]:
    lines = [f"## {title}"]
    if not findings:
        lines.append(empty)
        return lines
    for finding in findings:
        lines.append(f"- **{finding.code}**: {finding.title}")
        lines.append(f"  - {finding.detail}")
        if finding.files:
            lines.append(f"  - Files: {', '.join(finding.files)}")
    return lines


def render_structured_summary(result: ReviewResult, review_prompt: str = "") -> str:
    lines: list[str] = []
    if review_prompt.strip():
        lines.append("## Review prompt")
        lines.append(review_prompt.strip())
        lines.append("")
    if result.summary:
        lines.append("## Codex summary")
        lines.append(result.summary)
        lines.append("")
    lines.extend(render_section("Blocking findings", result.blocking, "- None"))
    lines.append("")
    lines.extend(render_section("Advisory warnings", result.warnings, "- None"))
    lines.append("")
    lines.append(f"## Backend\n- {result.backend}")
    lines.append(f"## Result\n- {'PASS' if result.passed else 'FAIL'}")
    return "\n".join(lines)


def build_review_comment(
    result: ReviewResult,
    *,
    sha: str = "",
    run_url: str = "",
    pr_number: int | None = None,
    review_prompt: str = "",
) -> str:
    header = [COMMENT_MARKER, "# Review Gate agent review"]
    if pr_number is not None:
        header.append(f"**PR:** #{pr_number}")
    if sha:
        header.append(f"**Head SHA:** `{sha[:8]}`")
    if run_url:
        header.append(f"**Run:** {run_url}")
    header.append("")
    footer = "_This comment is posted by Review Gate. No secrets are included._"
    return "\n".join([*header, render_structured_summary(result, review_prompt), "", footer])


def status_description(result: ReviewResult) -> str:
    if result.passed:
        return "Codex review passed - no blocking findings"
    return f"Codex review failed - {len(result.blocking)} blocking finding(s)"


def post_commit_status(
    repo: str,
    sha: str,
    state: str,
    description: str,
    target_url: str = "",
    context: str = STATUS_CONTEXT_THOROUGH,
) -> None:
    if not sha:
        return
    args = [
        "gh",
        "api",
        f"repos/{repo}/statuses/{sha}",
        "--method",
        "POST",
        "-f",
        f"state={state}",
        "-f",
        f"context={context}",
        "-f",
        f"description={description[:140]}",
    ]
    if target_url:
        args.extend(["-f", f"target_url={target_url}"])
    result = run_command(args)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())


def fetch_issue_comments(repo: str, pr_number: int) -> list[dict]:
    """All issue comments as one flat list, correct across pagination.

    `gh api --paginate` emits one JSON array PER PAGE; `--slurp` wraps those pages
    into an outer array. We flatten one level so multi-page PRs don't crash
    json.loads on concatenated arrays (and a single flat page still works).
    """
    result = run_command(
        ["gh", "api", f"repos/{repo}/issues/{pr_number}/comments", "--paginate", "--slurp"]
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    data = json.loads(result.stdout or "[]")
    comments: list[dict] = []
    for item in data:
        if isinstance(item, list):
            comments.extend(item)
        elif isinstance(item, dict):
            comments.append(item)
    return comments


def post_or_update_pr_comment(repo: str, pr_number: int, body: str) -> None:
    comment_id = None
    for comment in fetch_issue_comments(repo, pr_number):
        if COMMENT_MARKER in comment.get("body", ""):
            comment_id = comment["id"]
            break
    if comment_id:
        result = run_command(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/comments/{comment_id}",
                "--method",
                "PATCH",
                "-f",
                f"body={body}",
            ]
        )
    else:
        result = run_command(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{pr_number}/comments",
                "--method",
                "POST",
                "-f",
                f"body={body}",
            ]
        )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())


def finding_fingerprint(finding: Finding) -> str:
    basis = "\n".join([finding.severity, finding.title, finding.detail, *finding.files])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def build_finding_comment(finding: Finding, *, sha: str, run_url: str) -> str:
    marker = f"{FINDING_MARKER_PREFIX}{finding_fingerprint(finding)} -->"
    lines = [
        marker,
        f"## {finding.severity}: {finding.title}",
        "",
        finding.detail or "Codex did not provide additional detail.",
    ]
    if finding.files:
        lines.extend(["", f"Files: {', '.join(finding.files)}"])
    if sha:
        lines.extend(["", f"Head SHA: `{sha[:8]}`"])
    if run_url:
        lines.extend(["", f"Run: {run_url}"])
    return "\n".join(lines)


def post_finding_comments(
    repo: str,
    pr_number: int,
    result: ReviewResult,
    *,
    sha: str,
    run_url: str,
) -> None:
    findings = (*result.blocking, *result.warnings)
    existing = fetch_issue_comments(repo, pr_number)
    by_marker = {
        comment.get("body", "").split("-->", 1)[0] + "-->": comment["id"]
        for comment in existing
        if comment.get("body", "").startswith(FINDING_MARKER_PREFIX)
    }
    desired = {}
    for finding in findings:
        body = build_finding_comment(finding, sha=sha, run_url=run_url)
        desired[body.split("-->", 1)[0] + "-->"] = body
    for marker, comment_id in by_marker.items():
        if marker in desired:
            continue
        delete_result = run_command(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/comments/{comment_id}",
                "--method",
                "DELETE",
            ]
        )
        if delete_result.returncode != 0:
            raise RuntimeError(delete_result.stderr.strip())
    for marker, body in desired.items():
        comment_id = by_marker.get(marker)
        if comment_id:
            args = [
                "gh",
                "api",
                f"repos/{repo}/issues/comments/{comment_id}",
                "--method",
                "PATCH",
                "-f",
                f"body={body}",
            ]
        else:
            args = [
                "gh",
                "api",
                f"repos/{repo}/issues/{pr_number}/comments",
                "--method",
                "POST",
                "-f",
                f"body={body}",
            ]
        post_result = run_command(args)
        if post_result.returncode != 0:
            raise RuntimeError(post_result.stderr.strip())


def submit_pr_approval(repo: str, pr_number: int, body: str) -> bool:
    result = run_command(
        [
            "gh",
            "api",
            f"repos/{repo}/pulls/{pr_number}/reviews",
            "--method",
            "POST",
            "-f",
            "event=APPROVE",
            "-f",
            f"body={body}",
        ]
    )
    if result.returncode != 0:
        print(
            f"ERROR: could not submit PR approval: {result.stderr.strip()[:400]}",
            file=sys.stderr,
        )
        return False
    print(f"Submitted approving PR review on PR #{pr_number}.")
    return True


def expected_advisory_login() -> str | None:
    """The EXACT author login the gate's own advisory comments must carry.

    With a user/PAT token `gh api user` resolves the login directly. Under the
    GitHub App token (github.token) that endpoint is not accessible, so we use the
    configured expected bot login (default the Actions app, `github-actions[bot]`)
    -- an exact identity, never "any Bot". Returns None when no identity can be
    established, so callers fail closed to a full review.
    """
    result = run_command(["gh", "api", "user", "-q", ".login"])
    login = result.stdout.strip() if result.returncode == 0 else ""
    if login:
        return login
    configured = os.environ.get("REVIEW_GATE_BOT_LOGIN", "github-actions[bot]").strip()
    return configured or None


def trusted_fast_comment(comments: list[dict]) -> dict | None:
    """The latest advisory comment authored by the gate's EXACT identity, or None.

    Comments carrying the public marker but authored by any other login (another
    human, another app/bot) are ignored: delta focus and carried findings must
    never be steerable by anyone but the gate. Unknown identity -> None (full
    review), the fail-closed default.
    """
    expected = expected_advisory_login()
    if expected is None:
        return None
    trusted = None
    for comment in comments:
        if FAST_COMMENT_MARKER not in comment.get("body", ""):
            continue
        user = comment.get("user") or {}
        if user.get("login") == expected:
            trusted = comment
    return trusted


def _state_key() -> bytes | None:
    key = os.environ.get("REVIEW_GATE_STATE_KEY", "").strip()
    return key.encode("utf-8") if key else None


def sign_fast_state(sha: str | None, carried: tuple[str, ...]) -> str | None:
    """HMAC-signed `<b64(payload)>.<sig>` token, or None when unsigned/unavailable.

    Returns None when there is no signing key or no checkpoint SHA, so the comment
    carries no trusted state and the next round falls back to a full review.
    """
    key = _state_key()
    if key is None or not sha:
        return None
    payload = json.dumps(
        {"sha": sha, "carried": list(carried)}, sort_keys=True, separators=(",", ":")
    )
    b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    sig = hmac.new(key, b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def verify_fast_state(token: str) -> tuple[str | None, tuple[str, ...]]:
    """Verify a signed state token; (None, ()) if no key, bad signature, or garbage."""
    key = _state_key()
    if key is None or "." not in token:
        return None, ()
    b64, _, sig = token.partition(".")
    expected = hmac.new(key, b64.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None, ()
    try:
        payload = json.loads(base64.urlsafe_b64decode(b64.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None, ()
    sha = payload.get("sha")
    carried = tuple(str(t) for t in payload.get("carried", []) if str(t).strip())
    return (sha if isinstance(sha, str) and sha else None), carried


def read_fast_advisory_state(repo: str, pr_number: int) -> tuple[str | None, tuple[str, ...]]:
    """Return (last_reviewed_sha, prior_finding_titles) from the fast advisory comment.

    Fast mode reads only its OWN advisory comment (authored by the gate identity)
    so it never depends on, interferes with, or can be steered through the
    thorough gate's merge-authority comment surface or a forged participant comment.
    """
    try:
        comments = fetch_issue_comments(repo, pr_number)
    except RuntimeError:
        return None, ()
    trusted = trusted_fast_comment(comments)
    body = trusted.get("body", "") if trusted else ""
    if not body:
        return None, ()
    match = re.search(re.escape(STATE_MARKER_PREFIX) + r"\s*(\S+?)\s*-->", body)
    if not match:
        return None, ()
    # Trust the delta scope only if the signature verifies. Authorship (above) is
    # defense in depth; the signature is what makes the state unforgeable.
    return verify_fast_state(match.group(1))


def build_fast_comment(
    result: ReviewResult,
    *,
    sha: str,
    run_url: str,
    pr_number: int,
    delta_from_sha: str | None,
    checkpoint_sha: str | None,
    carried_forward: tuple[str, ...] = (),
) -> str:
    all_findings = (*result.blocking, *result.warnings)
    major = [f for f in all_findings if f.severity in FAST_ADVISORY_SEVERITIES]
    lines = [FAST_COMMENT_MARKER]
    # Signed machine state: the checkpoint SHA is advanced only to a
    # successfully-reviewed SHA (checkpoint_sha is the prior good SHA, or None, on
    # a failed round) and the carry-forward list rides along, all HMAC-signed so
    # it cannot be forged. Emitted only when a signing key is configured.
    signed = sign_fast_state(checkpoint_sha, carried_forward)
    if signed:
        lines.append(f"{STATE_MARKER_PREFIX}{signed} -->")
    lines += [
        "# Review Gate — fast advisory",
        "",
        "**Advisory only.** This is a fast, non-blocking pass to speed iteration. "
        "It does not approve, does not block merge, and is not merge evidence. The "
        "thorough `Review Gate` review remains required to merge.",
        f"**PR:** #{pr_number}",
    ]
    if sha:
        lines.append(f"**Head SHA:** `{sha[:8]}`")
    if delta_from_sha:
        lines.append(f"**Delta re-review since:** `{delta_from_sha[:8]}`")
    if run_url:
        lines.append(f"**Run:** {run_url}")
    lines.append("")
    if result.summary:
        lines.extend(["## Summary", result.summary, ""])
    lines.append("## Major findings (P0/P1)")
    if not major:
        lines.append("- None flagged in this fast pass. Run the thorough gate before merge.")
    else:
        for finding in major:
            lines.append(f"- **{finding.severity}**: {finding.title}")
            if finding.detail:
                lines.append(f"  - {finding.detail}")
            if finding.files:
                lines.append(f"  - Files: {', '.join(finding.files)}")
    # Display-only echo of the carry-forward list (the trusted copy is the signed
    # token above). Lets a human see what the next round will re-verify, including
    # findings preserved from a round whose review did not complete.
    if carried_forward:
        lines.append("## Carry-forward (re-verified next round)")
        for title in carried_forward:
            lines.append(f"- {title}")
    return "\n".join(lines)


def post_or_update_fast_comment(repo: str, pr_number: int, body: str) -> None:
    # Only ever edit our OWN advisory comment. If a participant forged the marker,
    # trusted_fast_comment returns None and we post a fresh gate-owned comment
    # rather than trying (and failing) to PATCH a comment we do not own.
    trusted = trusted_fast_comment(fetch_issue_comments(repo, pr_number))
    comment_id = trusted["id"] if trusted else None
    if comment_id:
        result = run_command(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/comments/{comment_id}",
                "--method",
                "PATCH",
                "-f",
                f"body={body}",
            ]
        )
    else:
        result = run_command(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{pr_number}/comments",
                "--method",
                "POST",
                "-f",
                f"body={body}",
            ]
        )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())


def run_fast_advisory(
    args: argparse.Namespace,
    review_prompt: str,
    *,
    workspace: Path,
) -> None:
    """Advisory lane wrapper: ALWAYS exits 0, even on operational failure.

    The fast lane is non-merge-authority; a nonzero job exit could be mistaken for
    a merge blocker. Any error fetching the base, calling the GitHub API, or
    running Codex is caught here, logged as a diagnostic, and reported (best
    effort) on the non-required advisory status, then the job exits 0.
    """
    try:
        _run_fast_advisory(args, review_prompt, workspace=workspace)
    except Exception as exc:  # noqa: BLE001 - advisory lane must never exit nonzero
        print(
            f"Fast advisory encountered an operational error (advisory, "
            f"non-blocking): {exc}",
            file=sys.stderr,
        )
        if args.post_status:
            try:
                post_commit_status(
                    args.repo,
                    args.sha,
                    "success",
                    "Fast advisory could not complete - advisory, not a merge gate",
                    args.run_url,
                    context=STATUS_CONTEXT_FAST,
                )
            except Exception as status_exc:  # noqa: BLE001 - best effort only
                print(f"(could not post advisory status: {status_exc})", file=sys.stderr)
    sys.exit(0)


def _run_fast_advisory(
    args: argparse.Namespace,
    review_prompt: str,
    *,
    workspace: Path,
) -> None:
    """Fast, advisory, non-merge-authority pass. Never approves, never blocks."""
    preflight = analyze_workspace(workspace, changed_files_for_pr(args.repo, args.pr))
    delta_from_sha, carried = read_fast_advisory_state(args.repo, args.pr)
    # Only treat the stored SHA as a delta base when it is a real, different
    # ancestor of the current head; otherwise fall back to a full review.
    if delta_from_sha:
        ancestor = run_command(
            ["git", "merge-base", "--is-ancestor", delta_from_sha, "HEAD"], cwd=workspace
        )
        if delta_from_sha == args.sha or ancestor.returncode != 0:
            delta_from_sha, carried = None, ()
    result = run_codex_review(
        workspace,
        review_prompt,
        repo=args.repo,
        pr_number=args.pr,
        sha=args.sha,
        base_ref=args.base_ref,
        mode="fast",
        delta_from_sha=delta_from_sha,
        carried_findings=carried,
    )
    # Deterministic preflight blockers (committed secrets, merge-conflict markers)
    # are always major in fast mode. Normalize their severity to P0 so the P0/P1
    # filter surfaces them and the advisory status reflects them; a fast pass must
    # never report "no major findings" while a secret or conflict marker is staged.
    promoted_blocking = tuple(
        Finding("P0", f.code, f.title, f.detail, f.files) for f in preflight.blocking
    )
    if promoted_blocking or preflight.warnings:
        result = ReviewResult(
            result.backend,
            summary=result.summary,
            warnings=(*preflight.warnings, *result.warnings),
            blocking=(*promoted_blocking, *result.blocking),
            raw_review=result.raw_review,
        )
    major = [
        f for f in (*result.blocking, *result.warnings) if f.severity in FAST_ADVISORY_SEVERITIES
    ]
    # If Codex did not produce a valid review, do NOT advance the checkpoint:
    # keep the prior successfully-reviewed SHA (None on the first round -> the
    # next run does a full review) so the failed round's diff is never skipped.
    review_failed = any(f.code in REVIEW_FAILURE_CODES for f in result.blocking)
    checkpoint_sha = delta_from_sha if review_failed else args.sha
    # Carry forward the re-verification list. A failed round produced no valid
    # findings, so it preserves the PRIOR round's carried list; a successful round
    # carries its own current major findings (minus the backend-failure markers).
    if review_failed:
        carried_forward = carried
    else:
        carried_forward = tuple(
            f.title for f in major if f.code not in REVIEW_FAILURE_CODES
        )
    print(
        f"Fast advisory review complete: {len(major)} major (P0/P1) finding(s)."
        + (" Review did not complete; checkpoint not advanced." if review_failed else "")
    )
    if args.post_comment:
        post_or_update_fast_comment(
            args.repo,
            args.pr,
            build_fast_comment(
                result,
                sha=args.sha,
                run_url=args.run_url,
                pr_number=args.pr,
                delta_from_sha=delta_from_sha,
                checkpoint_sha=checkpoint_sha,
                carried_forward=carried_forward,
            ),
        )
    if args.post_status:
        # Distinct, NON-required context. Never touches the "Review Gate" context
        # the merge gate owns. state=failure only signals P0/P1 to the author; a
        # non-required context cannot block merge.
        post_commit_status(
            args.repo,
            args.sha,
            "failure" if major else "success",
            f"Fast advisory: {len(major)} major finding(s) - not a merge gate"
            if major
            else "Fast advisory: no major findings - run thorough gate before merge",
            args.run_url,
            context=STATUS_CONTEXT_FAST,
        )
    # Returns normally; the run_fast_advisory wrapper performs the sys.exit(0) so
    # both the success and the operational-failure paths exit 0 identically.


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--sha", default="")
    parser.add_argument("--post-status", action="store_true")
    parser.add_argument("--post-comment", action="store_true")
    parser.add_argument("--submit-approval", action="store_true")
    parser.add_argument("--run-url", default="")
    parser.add_argument("--base-ref", default=os.environ.get("REVIEW_GATE_BASE_REF", "main"))
    parser.add_argument(
        "--mode",
        choices=("thorough", "fast"),
        default=os.environ.get("REVIEW_GATE_MODE", "thorough"),
        help="thorough = merge-authority full-diff review (default); "
        "fast = non-blocking advisory pass (delta re-review, P0/P1 only).",
    )
    args = parser.parse_args()

    review_prompt = os.environ.get("REVIEW_GATE_PROMPT", "").strip() or DEFAULT_REVIEW_PROMPT

    if args.mode == "fast":
        # Advisory lane: separate comment marker + status context, no approval,
        # always exits 0. Cannot touch the thorough merge-authority surface.
        run_fast_advisory(args, review_prompt, workspace=Path(args.workspace).resolve())
        return

    preflight = analyze_workspace(
        Path(args.workspace).resolve(),
        changed_files_for_pr(args.repo, args.pr),
    )
    if preflight.blocking:
        result = preflight
    else:
        result = run_codex_review(
            Path(args.workspace).resolve(),
            review_prompt,
            repo=args.repo,
            pr_number=args.pr,
            sha=args.sha,
            base_ref=args.base_ref,
        )
        if preflight.warnings:
            result = ReviewResult(
                result.backend,
                summary=result.summary,
                warnings=(*preflight.warnings, *result.warnings),
                blocking=result.blocking,
                raw_review=result.raw_review,
            )
    print(render_structured_summary(result, review_prompt))

    if args.post_comment:
        post_or_update_pr_comment(
            args.repo,
            args.pr,
            build_review_comment(
                result,
                sha=args.sha,
                run_url=args.run_url,
                pr_number=args.pr,
                review_prompt=review_prompt,
            ),
        )
        post_finding_comments(args.repo, args.pr, result, sha=args.sha, run_url=args.run_url)

    if result.passed and args.submit_approval:
        approved = submit_pr_approval(
            args.repo,
            args.pr,
            f"Codex review passed for {args.sha[:8]}. {status_description(result)}",
        )
        if not approved:
            if args.post_status:
                post_commit_status(
                    args.repo,
                    args.sha,
                    "failure",
                    "AI review passed but approval failed",
                    args.run_url,
                )
            sys.exit(1)

    if args.post_status:
        post_commit_status(
            args.repo,
            args.sha,
            "success" if result.passed else "failure",
            status_description(result),
            args.run_url,
        )
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
