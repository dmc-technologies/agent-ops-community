#!/usr/bin/env python3
"""Codex-backed PR review gate for label-triggered AI review workflows."""

from __future__ import annotations

import argparse
import base64
import binascii
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
RESOLUTION_STATE_MARKER_PREFIX = "<!-- review-gate-resolution-state:"
FOLLOW_UP_ISSUE_MARKER_PREFIX = "<!-- review-gate-follow-up:"
FOLLOW_UP_FINDING_MARKER_PREFIX = "<!-- review-gate-follow-up-finding:"
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
CRITICAL_CONSEQUENCE_CLASSES = {
    "acceptance_evidence",
    "core_behavior",
    "data_loss",
    "safety",
    "security",
}
NONCRITICAL_CONSEQUENCE_CLASSES = {
    "compatibility",
    "correctness",
    "data",
    "engineering",
    "operations",
    "security",
}
ALL_CONSEQUENCE_CLASSES = CRITICAL_CONSEQUENCE_CLASSES | NONCRITICAL_CONSEQUENCE_CLASSES
REVIEW_FAILURE_CODES = {
    "CODEX_REVIEW_FAILED",
    "CODEX_REVIEW_UNPARSEABLE",
    "CODEX_REVIEW_INVALID",
}
CRITICAL_PATH_RULES = (
    (
        "privileged_workflow",
        (
            ".github/",
            "scripts/release",
            "scripts/deploy",
            "dockerfile",
            "containerfile",
        ),
    ),
    (
        "ci_policy",
        ("configs/global/agents.md", "review-gate", "branch-protection", "branch_protection"),
    ),
    (
        "authorization",
        ("/auth/", "/auth.", "auth_", "authorization", "permission", "access_control"),
    ),
    ("credentials", ("credential", "secret", "token", "keyring")),
    ("irreversible_state", ("migration", "schema", "database", "storage", "persistence")),
    ("engineering_source_authority", ("source_authority", "source-authority", "provenance")),
    ("safety", ("safety", "allowable", "load_case", "load-case", "solver")),
    ("public_contract", ("openapi", "public_api", "public-api", "/api/", "protocol")),
    ("provider_boundary", ("provider", "adapter", "connector")),
    ("concurrency", ("concurrency", "locking", "mutex", "queue", "worker")),
    (
        "dependency_boundary",
        (
            "pyproject.toml",
            "requirements.txt",
            "requirements.lock",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "cargo.toml",
            "cargo.lock",
            "go.mod",
            "go.sum",
        ),
    ),
)
LITE_PATH_SUFFIXES = (
    ".md",
    ".rst",
    ".txt",
)
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
    scope: str = "lite"
    effort: str = "xhigh"
    confidence: str = "low"
    risk_domains: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    review_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolutionState:
    repo: str
    pr_number: int
    base_ref: str
    merge_base_sha: str
    sha: str
    scope: str
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class ReviewStage:
    name: str = "discovery"
    scope: str | None = None
    delta_from_sha: str | None = None
    carried_findings: tuple[Finding, ...] = ()


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


def changed_files_for_exact_head(
    workspace: Path,
    merge_base_sha: str,
    expected_head_sha: str,
) -> list[str]:
    head_result = run_command(["git", "rev-parse", "HEAD"], cwd=workspace)
    actual_head_sha = head_result.stdout.strip()
    if head_result.returncode != 0 or actual_head_sha != expected_head_sha:
        raise RuntimeError(
            "Checked-out head does not match the requested review head: "
            f"expected {expected_head_sha}, got {actual_head_sha or 'unavailable'}."
        )
    result = run_command(
        ["git", "diff", "--name-only", f"{merge_base_sha}...{expected_head_sha}"],
        cwd=workspace,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not list exact-head changed files.")
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
    if profile.scope == "critical":
        scope_instruction = (
            "Review the complete diff and affected trust, data, source-authority, "
            "public-contract, and operational boundaries."
        )
    else:
        scope_instruction = (
            "Review changed functions, their direct callers, and affected tests. "
            "Expand only when concrete evidence points to a critical consequence."
        )
    return f"""## Review profile

Scope: {profile.scope}
Reasoning effort: {profile.effort}
Risk domains: {domains}

Classification reasons:
{reasons}

Questions requiring focused review:
{questions}

{scope_instruction}

"""


def build_stage_section(
    stage: str,
    carried_findings: tuple[Finding, ...],
) -> str:
    if stage != "resolution":
        return """## Discovery review

This is the one discovery review for this pull request head. Find all verified
defects in the assigned scope in this pass and group instances by root cause.

"""
    carried = []
    for finding in carried_findings:
        files = ", ".join(finding.files) or "no file recorded"
        carried.append(
            f"- {finding.title} [root cause: {finding.root_cause or finding.code}; "
            f"files: {files}]\n  {finding.detail}"
        )
    carried_text = "\n".join(carried) or "- No carried critical finding was supplied."
    return f"""## Targeted resolution check

Do not repeat the discovery review. Check only:
- whether every carried critical finding below is resolved;
- the fix delta and directly affected callers or interfaces; and
- any new critical defect caused by the fix.

Do not search unchanged code for new findings. A verified noncritical defect in
the fix delta may be a follow-up; it must not block this resolution check.

Carried critical findings:
{carried_text}

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
    stage: str = "discovery",
    delta_from_sha: str | None = None,
    carried_findings: tuple[Finding, ...] = (),
) -> str:
    consequence_classes = (
        '"acceptance_evidence" | "compatibility" | "core_behavior" | '
        '"correctness" | "data" | "data_loss" | "engineering" | '
        '"operations" | "safety" | "security" | "none"'
    )
    profile_section = build_profile_section(profile)
    stage_section = build_stage_section(stage, carried_findings)
    diff_ref = delta_from_sha if stage == "resolution" and delta_from_sha else base_diff_ref
    return f"""You are executing the repository PR review gate.

Repository: {repo}
Pull request: #{pr_number}
Head SHA: {sha}
Base ref: {base_ref}

Use the local checkout as the source of truth. Review the PR diff with:

git diff {diff_ref}...HEAD

Apply this review prompt:

{review_prompt}

{profile_section}{stage_section}## Finding policy

Report every verified defect in the assigned scope in this one pass. Before returning:
- Re-scan the changes under review for additional INSTANCES of every issue class
  you found, but report one finding per root cause and include the affected files
  in that single finding.
- A finding is reportable only when the PR
  introduced an implementation defect that exists now, a plausible current path
  reaches that defect, the consequence and evidence are specific, and confidence
  is high.
- Use disposition=block only for a proven critical consequence: security,
  safety, data loss, broken core behavior, or false acceptance evidence. A
  sensitive change receives deeper review but does not block by category alone.
- Use disposition=follow_up for a verified noncritical defect. It will be filed
  in one follow-up issue and must not fail the gate.
- A missing test, proof, comment, or documentation is not a current implementation
  defect. Possible future regression is not a current failure path. If current
  behavior is correct, ignore the observation. If implementation behavior is
  incorrect, report that behavior as the defect; coverage may be part of the
  correction but never the root cause.
- Ignore style, naming, formatting, general refactoring, hypothetical future
  extensions, documentation maintenance without a broken operational path,
  missing coverage without a demonstrated material consequence, pre-existing
  issues, approval prerequisites, and failures already reported by deterministic
  checks or CI.
- Severity does not decide merge authority. The proven consequence does.

Return only valid JSON with this exact shape:
{{
  "verdict": "approve" | "comment" | "request_changes",
  "summary": "One concise paragraph on need, policy alignment, and merge readiness.",
  "findings": [
    {{
      "severity": "P0" | "P1" | "P2" | "P3",
      "disposition": "block" | "follow_up" | "ignore",
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
      "required_correction": "Required correction, follow-up resolution, or empty."
    }}
  ]
}}

Return request_changes only when at least one finding has disposition=block.
Return comment when findings contain only follow_up entries. Return approve when
there are no block or follow_up entries. Ignored observations are never posted.
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


def resolve_merge_base(workspace: Path, base_diff_ref: str) -> str:
    result = run_command(["git", "merge-base", base_diff_ref, "HEAD"], cwd=workspace)
    merge_base_sha = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", merge_base_sha):
        detail = result.stderr.strip() or "Could not resolve the pull-request merge base."
        raise RuntimeError(detail)
    return merge_base_sha


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def classify_review_profile(changed_files: list[str] | None) -> ReviewProfile:
    """Choose lite or critical scope without spending a separate model call."""
    if changed_files is None:
        return ReviewProfile(
            scope="critical",
            effort="xhigh",
            confidence="low",
            reasons=("Changed paths were unavailable, so review scope expanded.",),
            review_questions=("What trust, data, or public boundaries does the change affect?",),
        )

    domains: list[str] = []
    reasons: list[str] = []
    normalized = tuple(path.lower().replace("\\", "/") for path in changed_files)
    for domain, needles in CRITICAL_PATH_RULES:
        matched = sorted({path for path in normalized if any(needle in path for needle in needles)})
        if not matched:
            continue
        domains.append(domain)
        reasons.append(f"{domain} path changed: {', '.join(matched[:3])}")

    if domains:
        return ReviewProfile(
            scope="critical",
            effort="xhigh",
            confidence="high",
            risk_domains=tuple(domains),
            reasons=tuple(reasons),
            review_questions=tuple(
                f"Can the {domain.replace('_', ' ')} change cause a critical consequence?"
                for domain in domains
            ),
        )
    known_lite = bool(normalized) and all(
        path.endswith(LITE_PATH_SUFFIXES) for path in normalized
    )
    if known_lite:
        return ReviewProfile(
            scope="lite",
            effort="medium",
            confidence="high",
            reasons=("Every changed path matched the conservative lite allowlist.",),
            review_questions=("Do the changed documents state the supported behavior correctly?",),
        )
    return ReviewProfile(
        scope="critical",
        effort="xhigh",
        confidence="low",
        risk_domains=("unclassified_behavior",),
        reasons=("An unrecognized behavioral path changed, so review scope expanded.",),
        review_questions=("Can an unclassified behavior change cross a critical boundary?",),
    )


def apply_scope_policy(
    profile: ReviewProfile,
    *,
    requested_scope: str | None = None,
    prior_scope: str | None = None,
) -> ReviewProfile:
    """Apply explicit and prior scope without allowing a critical downgrade."""
    scopes = {profile.scope, requested_scope, prior_scope}
    scope = "critical" if "critical" in scopes else "lite"
    if scope == profile.scope:
        return profile
    reason = (
        "Review scope was explicitly raised to critical."
        if requested_scope == "critical"
        else "Resolution keeps the prior critical review scope."
    )
    return ReviewProfile(
        scope=scope,
        effort="xhigh" if scope == "critical" else "medium",
        confidence="high",
        risk_domains=profile.risk_domains,
        reasons=(reason, *profile.reasons),
        review_questions=profile.review_questions,
    )


def select_review_stage(
    workspace: Path,
    head_sha: str,
    prior_state: ResolutionState | None,
    *,
    repo: str,
    pr_number: int,
    base_ref: str,
    merge_base_sha: str,
) -> ReviewStage:
    if prior_state is None:
        return ReviewStage()
    if (
        prior_state.repo,
        prior_state.pr_number,
        prior_state.base_ref,
        prior_state.merge_base_sha,
    ) != (repo, pr_number, base_ref, merge_base_sha):
        return ReviewStage()
    persisted_scope = "critical" if prior_state.findings else prior_state.scope
    if prior_state.sha == head_sha:
        return ReviewStage(
            name="unchanged",
            scope=persisted_scope,
            delta_from_sha=prior_state.sha,
            carried_findings=prior_state.findings,
        )
    ancestor = run_command(
        ["git", "merge-base", "--is-ancestor", prior_state.sha, "HEAD"], cwd=workspace
    )
    if ancestor.returncode != 0:
        return ReviewStage()
    return ReviewStage(
        name="resolution",
        scope=persisted_scope,
        delta_from_sha=prior_state.sha,
        carried_findings=prior_state.findings,
    )


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
    if disposition not in {"block", "follow_up", "ignore"}:
        return False
    if disposition == "ignore":
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


def finding_from_payload(payload: dict, index: int) -> tuple[str, Finding] | None:
    requested_disposition = str(payload.get("disposition") or "").lower()
    if requested_disposition == "ignore":
        return None
    severity = str(payload.get("severity") or "P2").upper()
    if severity not in {"P0", "P1", "P2", "P3"}:
        severity = "P2"
    consequence_class = str(payload.get("consequence_class") or "none").lower()
    confidence = str(payload.get("confidence") or "low").lower()
    already_caught = str(payload.get("already_caught_by") or "").strip().lower()
    verified = (
        payload.get("introduced_by_pr") is True
        and confidence == "high"
        and consequence_class in ALL_CONSEQUENCE_CLASSES
        and already_caught == "none"
        and bool(str(payload.get("current_behavior_defect") or "").strip())
        and bool(str(payload.get("current_failure_path") or "").strip())
        and bool(str(payload.get("consequence") or "").strip())
    )
    if not verified:
        return None
    disposition = "block" if (
        requested_disposition == "block"
        and consequence_class in CRITICAL_CONSEQUENCE_CLASSES
    ) else "follow_up"
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
    return (
        disposition,
        Finding(
            severity,
            f"CODEX_{severity}_{index + 1}",
            title,
            detail,
            files,
            root_cause,
        ),
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
    stage: str = "discovery",
    delta_from_sha: str | None = None,
    carried_findings: tuple[Finding, ...] = (),
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
        stage=stage,
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
    classified_findings = tuple(
        classified
        for index, item in enumerate(findings_payload)
        if (classified := finding_from_payload(item, index)) is not None
    )
    blocking = group_root_cause_findings(
        tuple(finding for disposition, finding in classified_findings if disposition == "block")
    )
    warnings = group_root_cause_findings(
        tuple(
            finding
            for disposition, finding in classified_findings
            if disposition == "follow_up"
        )
    )
    blocking_root_causes = {finding.root_cause for finding in blocking if finding.root_cause}
    warnings = tuple(
        finding
        for finding in warnings
        if not finding.root_cause or finding.root_cause not in blocking_root_causes
    )
    return ReviewResult(
        "codex",
        summary=summary,
        warnings=warnings,
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
    changed_files: list[str] | None = None,
    stage: str = "discovery",
    delta_from_sha: str | None = None,
    carried_findings: tuple[Finding, ...] = (),
    forced_scope: str | None = None,
    prior_scope: str | None = None,
    base_diff_ref: str | None = None,
) -> ReviewResult:
    base_diff_ref = base_diff_ref or ensure_base_ref(workspace, repo, base_ref)
    profile = classify_review_profile(changed_files)
    profile = apply_scope_policy(
        profile,
        requested_scope=forced_scope,
        prior_scope=prior_scope,
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
        stage=stage,
        delta_from_sha=delta_from_sha,
        carried_findings=carried_findings,
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
        lines.append(f"- Scope: {result.profile.scope}")
        lines.append(f"- Reasoning effort: {result.profile.effort}")
        lines.append(f"- Scope confidence: {result.profile.confidence}")
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


def _state_key() -> bytes | None:
    key = os.environ.get("REVIEW_GATE_STATE_KEY", "").strip()
    return key.encode("utf-8") if key else None


def _finding_payload(finding: Finding) -> dict:
    return {
        "severity": finding.severity,
        "code": finding.code,
        "title": finding.title,
        "detail": finding.detail,
        "files": list(finding.files),
        "root_cause": finding.root_cause,
    }


def _encoded_resolution_payload(state: ResolutionState) -> str:
    payload = json.dumps(
        {
            "repo": state.repo,
            "pr_number": state.pr_number,
            "base_ref": state.base_ref,
            "merge_base_sha": state.merge_base_sha,
            "sha": state.sha,
            "scope": state.scope,
            "findings": [_finding_payload(finding) for finding in state.findings],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def sign_resolution_state(state: ResolutionState) -> str:
    encoded = _encoded_resolution_payload(state)
    key = _state_key()
    if key is None:
        return f"unsigned.{encoded}"
    signature = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_resolution_state(
    token: str,
    *,
    allow_trusted_unsigned: bool = False,
) -> ResolutionState | None:
    key = _state_key()
    if token.startswith("unsigned."):
        if key is not None or not allow_trusted_unsigned:
            return None
        encoded = token.removeprefix("unsigned.")
    elif key is None or "." not in token:
        return None
    else:
        encoded, _, signature = token.partition(".")
        expected = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
        repo = str(payload["repo"]).strip()
        pr_number = int(payload["pr_number"])
        base_ref = str(payload["base_ref"]).strip()
        merge_base_sha = str(payload["merge_base_sha"]).strip()
        sha = str(payload["sha"]).strip()
        scope = str(payload["scope"]).strip()
        raw_findings = payload["findings"]
        if (
            not repo
            or pr_number < 1
            or not base_ref
            or not merge_base_sha
            or not sha
            or scope not in {"lite", "critical"}
            or not isinstance(raw_findings, list)
        ):
            return None
        findings = tuple(
            Finding(
                severity=str(item["severity"]),
                code=str(item["code"]),
                title=str(item["title"]),
                detail=str(item["detail"]),
                files=tuple(str(path) for path in item.get("files", [])),
                root_cause=str(item.get("root_cause") or ""),
            )
            for item in raw_findings
            if isinstance(item, dict)
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, binascii.Error):
        return None
    if not findings:
        return None
    return ResolutionState(
        repo=repo,
        pr_number=pr_number,
        base_ref=base_ref,
        merge_base_sha=merge_base_sha,
        sha=sha,
        scope=scope,
        findings=findings,
    )


def build_review_comment(
    result: ReviewResult,
    *,
    sha: str = "",
    run_url: str = "",
    pr_number: int | None = None,
    review_prompt: str = "",
    resolution_state: ResolutionState | None = None,
) -> str:
    header = [COMMENT_MARKER, "# Review Gate agent review"]
    if resolution_state:
        signed_state = sign_resolution_state(resolution_state)
        header.append(f"{RESOLUTION_STATE_MARKER_PREFIX}{signed_state} -->")
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


def expected_gate_login() -> str | None:
    result = run_command(["gh", "api", "user", "-q", ".login"])
    login = result.stdout.strip() if result.returncode == 0 else ""
    if login:
        return login
    configured = os.environ.get("REVIEW_GATE_BOT_LOGIN", "github-actions[bot]").strip()
    return configured or None


def read_resolution_state(repo: str, pr_number: int) -> ResolutionState | None:
    try:
        comments = fetch_issue_comments(repo, pr_number)
    except RuntimeError:
        return None
    expected_login = expected_gate_login()
    for comment in reversed(comments):
        body = str(comment.get("body") or "")
        lines = body.splitlines()
        if len(lines) < 2 or lines[0] != COMMENT_MARKER or lines[1] != "# Review Gate agent review":
            continue
        state_line = lines[2] if len(lines) > 2 else ""
        match = re.fullmatch(
            re.escape(RESOLUTION_STATE_MARKER_PREFIX) + r"(\S+?) -->", state_line
        )
        if match:
            token = match.group(1)
            author = str((comment.get("user") or {}).get("login") or "")
            if expected_login is None or author != expected_login:
                continue
            if token.startswith("unsigned."):
                return verify_resolution_state(token, allow_trusted_unsigned=True)
            return verify_resolution_state(token)
        return None
    return None


def post_or_update_pr_comment(repo: str, pr_number: int, body: str) -> None:
    comment_id = None
    expected_login = expected_gate_login()
    for comment in fetch_issue_comments(repo, pr_number):
        author = str((comment.get("user") or {}).get("login") or "")
        body = str(comment.get("body") or "")
        exact_header = body.splitlines()[:2] == [
            COMMENT_MARKER,
            "# Review Gate agent review",
        ]
        if exact_header and author == expected_login:
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


def build_follow_up_issue_body(
    repo: str,
    pr_number: int,
    sha: str,
    findings: tuple[Finding, ...],
) -> str:
    marker = f"{FOLLOW_UP_ISSUE_MARKER_PREFIX}{repo}#{pr_number} -->"
    lines = [
        marker,
        f"# Follow-up findings from PR #{pr_number}",
        "",
        "These verified findings are noncritical. They did not block merge and "
        "can be resolved as normal issue work.",
        "",
        f"Reviewed head: `{sha}`",
        "",
    ]
    for finding in findings:
        encoded_finding = base64.urlsafe_b64encode(
            json.dumps(
                _finding_payload(finding),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        lines.extend(
            [
                f"{FOLLOW_UP_FINDING_MARKER_PREFIX}{encoded_finding} -->",
                f"## {finding.title}",
                "",
                finding.detail,
            ]
        )
        if finding.files:
            lines.extend(["", f"Files: {', '.join(finding.files)}"])
        lines.append("")
    return "\n".join(lines).rstrip()


def findings_from_follow_up_issue(body: str) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    pattern = re.escape(FOLLOW_UP_FINDING_MARKER_PREFIX) + r"\s*(\S+?)\s*-->"
    for match in re.finditer(pattern, body):
        try:
            payload = json.loads(
                base64.urlsafe_b64decode(match.group(1).encode("ascii")).decode("utf-8")
            )
            finding = Finding(
                severity=str(payload["severity"]),
                code=str(payload["code"]),
                title=str(payload["title"]),
                detail=str(payload["detail"]),
                files=tuple(str(path) for path in payload.get("files", [])),
                root_cause=str(payload.get("root_cause") or ""),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, binascii.Error):
            continue
        if finding.title and finding.detail:
            findings.append(finding)
    return tuple(findings)


def merge_follow_up_findings(
    prior: tuple[Finding, ...],
    current: tuple[Finding, ...],
) -> tuple[Finding, ...]:
    merged: dict[str, Finding] = {}
    for finding in (*prior, *current):
        key = finding.root_cause or finding_fingerprint(finding)
        merged[key] = finding
    return tuple(merged.values())


def post_or_update_follow_up_issue(
    repo: str,
    pr_number: int,
    sha: str,
    findings: tuple[Finding, ...],
) -> str:
    if not findings:
        return ""
    marker = f"{FOLLOW_UP_ISSUE_MARKER_PREFIX}{repo}#{pr_number} -->"
    list_result = run_command(
        [
            "gh",
            "api",
            f"repos/{repo}/issues",
            "--method",
            "GET",
            "--paginate",
            "--slurp",
            "-f",
            "state=all",
            "-f",
            "per_page=100",
        ]
    )
    if list_result.returncode != 0:
        raise RuntimeError(list_result.stderr.strip())
    raw_issues = json.loads(list_result.stdout or "[]")
    issues: list[dict] = []
    for item in raw_issues:
        if isinstance(item, list):
            issues.extend(candidate for candidate in item if isinstance(candidate, dict))
        elif isinstance(item, dict):
            issues.append(item)
    expected_login = expected_gate_login()
    existing = next(
        (
            issue
            for issue in issues
            if str(issue.get("body") or "").splitlines()[:1] == [marker]
            and "pull_request" not in issue
            and expected_login is not None
            and str((issue.get("user") or {}).get("login") or "") == expected_login
        ),
        None,
    )
    if existing:
        findings = merge_follow_up_findings(
            findings_from_follow_up_issue(str(existing.get("body") or "")),
            findings,
        )
    body = build_follow_up_issue_body(repo, pr_number, sha, findings)
    title = f"Review follow-up from PR #{pr_number}"
    if existing:
        issue_number = existing.get("number")
        args = [
            "gh",
            "api",
            f"repos/{repo}/issues/{issue_number}",
            "--method",
            "PATCH",
            "-f",
            f"title={title}",
            "-f",
            f"body={body}",
        ]
    else:
        args = [
            "gh",
            "api",
            f"repos/{repo}/issues",
            "--method",
            "POST",
            "-f",
            f"title={title}",
            "-f",
            f"body={body}",
        ]
    result = run_command(args)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    payload = json.loads(result.stdout or "{}")
    existing_url = existing.get("html_url") if existing else ""
    return str(payload.get("html_url") or existing_url)


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
    expected_login = expected_gate_login()
    by_marker = {
        comment.get("body", "").split("-->", 1)[0] + "-->": comment["id"]
        for comment in existing
        if comment.get("body", "").startswith(FINDING_MARKER_PREFIX)
        and str((comment.get("user") or {}).get("login") or "") == expected_login
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
    parser.add_argument(
        "--scope",
        choices=("auto", "lite", "critical"),
        default="auto",
        help=(
            "auto selects scope from changed paths; lite cannot lower detected or prior "
            "critical scope"
        ),
    )
    args = parser.parse_args()

    review_prompt = os.environ.get("REVIEW_GATE_PROMPT", "").strip() or DEFAULT_REVIEW_PROMPT
    workspace = Path(args.workspace).resolve()
    base_diff_ref = ensure_base_ref(workspace, args.repo, args.base_ref)
    merge_base_sha = resolve_merge_base(workspace, base_diff_ref)
    changed_files = changed_files_for_exact_head(workspace, merge_base_sha, args.sha)
    profile = classify_review_profile(changed_files)
    requested_scope = args.scope if args.scope in {"lite", "critical"} else None
    profile = apply_scope_policy(profile, requested_scope=requested_scope)
    prior_state = read_resolution_state(args.repo, args.pr)
    stage = select_review_stage(
        workspace,
        args.sha,
        prior_state,
        repo=args.repo,
        pr_number=args.pr,
        base_ref=args.base_ref,
        merge_base_sha=merge_base_sha,
    )
    profile = apply_scope_policy(profile, prior_scope=stage.scope)

    preflight = analyze_workspace(workspace, changed_files)
    model_follow_ups: tuple[Finding, ...] = ()
    if preflight.blocking:
        result = ReviewResult(
            preflight.backend,
            warnings=preflight.warnings,
            blocking=preflight.blocking,
            profile=profile,
        )
    elif stage.name == "unchanged":
        result = ReviewResult(
            "prior-review",
            summary="The reviewed head is unchanged; prior critical findings still apply.",
            blocking=stage.carried_findings,
            profile=profile,
        )
    else:
        result = run_routed_codex_review(
            workspace,
            review_prompt,
            repo=args.repo,
            pr_number=args.pr,
            sha=args.sha,
            base_ref=args.base_ref,
            changed_files=changed_files,
            stage=stage.name,
            delta_from_sha=stage.delta_from_sha,
            carried_findings=stage.carried_findings,
            forced_scope=profile.scope,
            prior_scope=stage.scope,
            base_diff_ref=base_diff_ref,
        )
        model_follow_ups = result.warnings
        if preflight.warnings:
            result = ReviewResult(
                result.backend,
                summary=result.summary,
                warnings=(*preflight.warnings, *result.warnings),
                blocking=result.blocking,
                raw_review=result.raw_review,
                profile=result.profile,
            )

    review_failed = any(finding.code in REVIEW_FAILURE_CODES for finding in result.blocking)
    if review_failed:
        next_state = prior_state
    elif result.blocking:
        next_state = ResolutionState(
            repo=args.repo,
            pr_number=args.pr,
            base_ref=args.base_ref,
            merge_base_sha=merge_base_sha,
            sha=args.sha,
            scope="critical",
            findings=result.blocking,
        )
    else:
        next_state = None
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
                resolution_state=next_state,
            ),
        )
        post_finding_comments(args.repo, args.pr, result, sha=args.sha, run_url=args.run_url)
        if model_follow_ups:
            try:
                issue_url = post_or_update_follow_up_issue(
                    args.repo, args.pr, args.sha, model_follow_ups
                )
                if issue_url:
                    print(f"Recorded noncritical findings in {issue_url}.")
            except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
                print(
                    f"WARNING: noncritical follow-up issue could not be recorded: {exc}",
                    file=sys.stderr,
                )

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
