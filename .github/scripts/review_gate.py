#!/usr/bin/env python3
"""Codex-backed PR review gate for label-triggered AI review workflows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

COMMENT_MARKER = "<!-- review-gate-agent-review -->"
FINDING_MARKER_PREFIX = "<!-- review-gate-finding:"
STATUS_CONTEXT_THOROUGH = "Review Gate"
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
REVIEW_EFFORTS = ("low", "medium", "high", "xhigh")
CRITICAL_RISK_DOMAINS = {
    "authorization",
    "credentials",
    "engineering_source_authority",
    "irreversible_state",
    "privileged_workflow",
    "safety",
}
HIGH_RISK_DOMAINS = {
    "ci_policy",
    "concurrency",
    "persistence",
    "provider_boundary",
    "public_contract",
}
MATERIAL_CONSEQUENCE_CLASSES = {
    "compatibility",
    "correctness",
    "data",
    "engineering",
    "operations",
    "security",
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
    root_cause: str = ""


@dataclass(frozen=True)
class ReviewProfile:
    effort: str = "xhigh"
    confidence: str = "low"
    risk_domains: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    review_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewResult:
    backend: str
    summary: str = ""
    warnings: tuple[Finding, ...] = ()
    blocking: tuple[Finding, ...] = ()
    raw_review: str = ""
    profile: ReviewProfile | None = None

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
        "TEMP",
        "TMPDIR",
        "USER",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def codex_model_args(*, effort: str = "medium") -> list[str]:
    if effort not in REVIEW_EFFORTS:
        effort = "xhigh"
    return [
        "--model",
        DEFAULT_CODEX_MODEL,
        "-c",
        f'model_reasoning_effort="{effort}"',
    ]


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


def build_profile_section(profile: ReviewProfile | None) -> str:
    if profile is None:
        return ""
    domains = ", ".join(profile.risk_domains) or "none identified"
    reasons = "\n".join(f"- {reason}" for reason in profile.reasons) or "- None supplied"
    questions = (
        "\n".join(f"- {question}" for question in profile.review_questions)
        or "- Apply the repository review prompt to the complete diff."
    )
    return f"""## Classified review profile

Reasoning effort: {profile.effort}
Risk domains: {domains}

Classification reasons:
{reasons}

Questions requiring focused review:
{questions}

The profile focuses the review; it does not narrow the diff and it is not
authoritative. Report a material defect outside these domains when the evidence
requires it.

"""


def build_codex_prompt(
    review_prompt: str,
    *,
    repo: str,
    pr_number: int,
    sha: str,
    base_ref: str,
    base_diff_ref: str,
    profile: ReviewProfile | None = None,
) -> str:
    consequence_classes = (
        '"compatibility" | "correctness" | "data" | "engineering" | '
        '"operations" | "security" | "none"'
    )
    profile_section = build_profile_section(profile)
    return f"""You are executing the repository PR review gate.

Repository: {repo}
Pull request: #{pr_number}
Head SHA: {sha}
Base ref: {base_ref}

Use the local checkout as the source of truth. Review the PR diff with:

git diff {base_diff_ref}...HEAD

Apply this review prompt:

{review_prompt}

{profile_section}## Complete material-defect search

Enumerate EVERY blocking finding present in the diff scope shown above in this
one pass. Do not defer or ration findings for a later review round --
assume there is no cheap later round and that omitted issues ship. Before
returning:
- Re-scan the changes under review for additional INSTANCES of every issue class
  you found, but report one finding per root cause and include the affected files
  in that single finding.
- Investigate broadly in reasoning. A finding is reportable only when the PR
  introduced an implementation defect that exists now, a plausible current path
  reaches that defect, it has a concrete material consequence, the evidence is
  specific, confidence is high, and a correction is required before merge.
- A missing test, proof, comment, or documentation is not a current implementation
  defect. Possible future regression is not a current failure path. If current
  behavior is correct, suppress the observation. If implementation behavior is
  incorrect, report that behavior as the defect; coverage may be part of the
  correction but never the root cause.
- Suppress style, naming, formatting, general refactoring, hypothetical future
  extensions, documentation maintenance without a broken operational path,
  missing coverage without a demonstrated material consequence, pre-existing
  issues, approval prerequisites, and failures already reported by deterministic
  checks or CI.
- P3 can never block. A P2 finding blocks only when it independently satisfies
  every materiality field below.

Return only valid JSON with this exact shape:
{{
  "verdict": "approve" | "comment" | "request_changes",
  "summary": "One concise paragraph on need, policy alignment, and merge readiness.",
  "findings": [
    {{
      "severity": "P0" | "P1" | "P2" | "P3",
      "disposition": "block" | "suppress",
      "title": "Short imperative finding title",
      "body": "Specific evidence from the changed code.",
      "files": ["relative/path.ext"],
      "introduced_by_pr": true | false,
      "current_behavior_defect": "Specific changed behavior that is incorrect now.",
      "current_failure_path": "Plausible current path that reaches the behavior.",
      "consequence_class": {consequence_classes},
      "consequence": "Concrete consequence, or empty when suppressed.",
      "confidence": "high" | "medium" | "low",
      "root_cause": "Stable identifier shared by related instances.",
      "already_caught_by": "none" | "CI/check name or explicit prerequisite",
      "required_correction": "Correction required before merge, or empty when suppressed."
    }}
  ]
}}

Use disposition=suppress for observations that fail any materiality condition.
Suppressed observations are retained only for shadow evaluation and are never
posted to the pull request. If there are no reportable findings, return
"approve"; the findings array may contain suppressed observations.
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


def build_classification_prompt(
    *,
    repo: str,
    pr_number: int,
    sha: str,
    base_ref: str,
    base_diff_ref: str,
) -> str:
    return f"""Classify impact and review difficulty for this pull request.

Repository: {repo}
Pull request: #{pr_number}
Head SHA: {sha}
Base ref: {base_ref}

Inspect the complete change with:

git diff {base_diff_ref}...HEAD

This is classification, not code review. Do not report defects and do not
suggest fixes. Identify the effects that could make a small change consequential
and choose the lowest Sol reasoning effort that can reliably review them.

Routing rules:
- low: clearly mechanical, generated, or presentation-only changes whose source
  authority and behavior are unchanged.
- medium: contained behavior with familiar local failure modes and no authority,
  persistence, public-contract, concurrency, or engineering-source change.
- high: cross-component behavior, persistence, public contracts, provider
  behavior, concurrency, CI policy, or uncertain scope.
- xhigh: authorization, credentials, privileged execution, irreversible state,
  engineering source authority, safety decisions, or difficult novel behavior.
- Low confidence must route to xhigh. Change size never lowers effort.

Return only valid JSON:
{{
  "recommended_effort": "low" | "medium" | "high" | "xhigh",
  "confidence": "high" | "medium" | "low",
  "risk_domains": ["lower_snake_case_domain"],
  "reasons": ["Concrete changed behavior used for routing."],
  "review_questions": ["Question the full reviewer must answer."]
}}
"""


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def review_profile_from_payload(payload: dict) -> ReviewProfile:
    effort = str(payload.get("recommended_effort") or "").lower()
    confidence = str(payload.get("confidence") or "").lower()
    reasons = _string_tuple(payload.get("reasons"))
    questions = _string_tuple(payload.get("review_questions"))
    valid = (
        effort in REVIEW_EFFORTS
        and confidence in {"high", "medium", "low"}
        and isinstance(payload.get("risk_domains"), list)
        and bool(reasons)
        and bool(questions)
    )
    if not valid:
        return ReviewProfile(
            effort="xhigh",
            confidence="low",
            reasons=("Classification output was incomplete; routed fail-closed to xhigh.",),
        )
    domains = tuple(
        domain.lower().replace("-", "_")
        for domain in _string_tuple(payload.get("risk_domains"))
    )
    if confidence == "low" or CRITICAL_RISK_DOMAINS.intersection(domains):
        effort = "xhigh"
    elif HIGH_RISK_DOMAINS.intersection(domains) and REVIEW_EFFORTS.index(effort) < 2:
        effort = "high"
    return ReviewProfile(
        effort=effort,
        confidence=confidence,
        risk_domains=domains,
        reasons=reasons,
        review_questions=questions,
    )


def run_codex_classification(
    workspace: Path,
    *,
    repo: str,
    pr_number: int,
    sha: str,
    base_ref: str,
    base_diff_ref: str | None = None,
) -> ReviewProfile:
    base_diff_ref = base_diff_ref or ensure_base_ref(workspace, repo, base_ref)
    output_path = workspace / ".review-gate-classification.json"
    prompt = build_classification_prompt(
        repo=repo,
        pr_number=pr_number,
        sha=sha,
        base_ref=base_ref,
        base_diff_ref=base_diff_ref,
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
            *codex_model_args(effort="low"),
        ],
        cwd=workspace,
        env=codex_child_env(),
        input_text=prompt,
    )
    raw = output_path.read_text(encoding="utf-8") if output_path.exists() else result.stdout
    output_path.unlink(missing_ok=True)
    if result.returncode != 0:
        return ReviewProfile(
            effort="xhigh",
            confidence="low",
            reasons=("Classification did not complete; routed fail-closed to xhigh.",),
        )
    try:
        payload = extract_json_object(raw)
    except (ValueError, json.JSONDecodeError):
        return ReviewProfile(
            effort="xhigh",
            confidence="low",
            reasons=("Classification was invalid; routed fail-closed to xhigh.",),
        )
    return review_profile_from_payload(payload)


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
    """A finding must satisfy the materiality output contract."""
    if not isinstance(item, dict):
        return False
    for field in (
        "severity",
        "disposition",
        "title",
        "body",
        "detail",
        "current_behavior_defect",
        "current_failure_path",
        "consequence_class",
        "consequence",
        "confidence",
        "root_cause",
        "already_caught_by",
        "required_correction",
    ):
        value = item.get(field)
        if value is not None and not isinstance(value, str):
            return False
    files = item.get("files")
    introduced = item.get("introduced_by_pr")
    if files is not None and not isinstance(files, list):
        return False
    if introduced is not None and not isinstance(introduced, bool):
        return False
    disposition = str(item.get("disposition") or "").lower()
    if disposition not in {"block", "suppress"}:
        return False
    if disposition == "suppress":
        return True
    required_strings = (
        "severity",
        "title",
        "current_behavior_defect",
        "current_failure_path",
        "consequence_class",
        "consequence",
        "confidence",
        "root_cause",
        "already_caught_by",
        "required_correction",
    )
    return introduced is not None and all(
        str(item.get(field) or "").strip() for field in required_strings
    )


def finding_from_payload(payload: dict, index: int) -> Finding | None:
    if str(payload.get("disposition") or "").lower() != "block":
        return None
    severity = str(payload.get("severity") or "P2").upper()
    if severity not in {"P0", "P1", "P2", "P3"}:
        severity = "P2"
    consequence_class = str(payload.get("consequence_class") or "none").lower()
    confidence = str(payload.get("confidence") or "low").lower()
    already_caught = str(payload.get("already_caught_by") or "").strip().lower()
    reportable = (
        severity != "P3"
        and payload.get("introduced_by_pr") is True
        and confidence == "high"
        and consequence_class in MATERIAL_CONSEQUENCE_CLASSES
        and already_caught == "none"
    )
    if not reportable:
        return None
    title = str(payload.get("title") or f"Codex finding {index + 1}").strip()
    evidence = str(payload.get("body") or payload.get("detail") or "").strip()
    failure_path = str(payload.get("current_failure_path") or "").strip()
    consequence = str(payload.get("consequence") or "").strip()
    correction = str(payload.get("required_correction") or "").strip()
    detail = "\n".join(
        (
            f"Behavior: {failure_path}",
            f"Consequence: {consequence}",
            f"Required correction: {correction}",
            f"Evidence: {evidence}",
        )
    )
    files_payload = payload.get("files") or []
    files = tuple(str(item) for item in files_payload if str(item).strip())
    root_cause = str(payload.get("root_cause") or "").strip()
    return Finding(
        severity,
        f"CODEX_{severity}_{index + 1}",
        title,
        detail,
        files,
        root_cause,
    )


def group_root_cause_findings(findings: tuple[Finding, ...]) -> tuple[Finding, ...]:
    grouped: dict[str, Finding] = {}
    order: list[str] = []
    for finding in findings:
        key = finding.root_cause or finding.code
        if key not in grouped:
            grouped[key] = finding
            order.append(key)
            continue
        existing = grouped[key]
        details = existing.detail
        if finding.detail not in details:
            details = f"{details}\n\nAdditional instance:\n{finding.detail}"
        grouped[key] = Finding(
            existing.severity,
            existing.code,
            existing.title,
            details,
            tuple(dict.fromkeys((*existing.files, *finding.files))),
            key,
        )
    return tuple(grouped[key] for key in order)


def run_codex_review(
    workspace: Path,
    review_prompt: str,
    *,
    repo: str,
    pr_number: int,
    sha: str,
    base_ref: str,
    profile: ReviewProfile | None = None,
    base_diff_ref: str | None = None,
) -> ReviewResult:
    base_diff_ref = base_diff_ref or ensure_base_ref(workspace, repo, base_ref)
    output_path = workspace / ".review-gate-codex-output.json"
    prompt = build_codex_prompt(
        review_prompt,
        repo=repo,
        pr_number=pr_number,
        sha=sha,
        base_ref=base_ref,
        base_diff_ref=base_diff_ref,
        profile=profile,
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
            *codex_model_args(effort=profile.effort if profile else "medium"),
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
    # a clean review: otherwise it can produce zero blockers and silently pass.
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
    summary = str(payload.get("summary") or "").strip()
    findings = tuple(
        finding
        for index, item in enumerate(findings_payload)
        if (finding := finding_from_payload(item, index)) is not None
    )
    blocking = group_root_cause_findings(findings)
    return ReviewResult(
        "codex",
        summary=summary,
        blocking=blocking,
        raw_review=raw_review,
        profile=profile,
    )


def run_routed_codex_review(
    workspace: Path,
    review_prompt: str,
    *,
    repo: str,
    pr_number: int,
    sha: str,
    base_ref: str,
) -> ReviewResult:
    base_diff_ref = ensure_base_ref(workspace, repo, base_ref)
    profile = run_codex_classification(
        workspace,
        repo=repo,
        pr_number=pr_number,
        sha=sha,
        base_ref=base_ref,
        base_diff_ref=base_diff_ref,
    )
    return run_codex_review(
        workspace,
        review_prompt,
        repo=repo,
        pr_number=pr_number,
        sha=sha,
        base_ref=base_ref,
        profile=profile,
        base_diff_ref=base_diff_ref,
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
    if result.profile:
        lines.append("## Review profile")
        lines.append(f"- Model: {DEFAULT_CODEX_MODEL}")
        lines.append(f"- Reasoning effort: {result.profile.effort}")
        lines.append(f"- Classifier confidence: {result.profile.confidence}")
        domains = ", ".join(result.profile.risk_domains) or "none identified"
        lines.append(f"- Risk domains: {domains}")
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
    findings = result.blocking
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


def submit_pr_approval(repo: str, pr_number: int, sha: str, body: str) -> bool:
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
            f"commit_id={sha}",
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
    args = parser.parse_args()

    review_prompt = os.environ.get("REVIEW_GATE_PROMPT", "").strip() or DEFAULT_REVIEW_PROMPT

    preflight = analyze_workspace(
        Path(args.workspace).resolve(),
        changed_files_for_pr(args.repo, args.pr),
    )
    if preflight.blocking:
        result = preflight
    else:
        result = run_routed_codex_review(
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
                profile=result.profile,
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
            args.sha,
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
