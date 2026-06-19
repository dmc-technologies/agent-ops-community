#!/usr/bin/env python3
"""Deterministic PR review gate for label-triggered AI review workflows."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

COMMENT_MARKER = "<!-- review-gate-agent-review -->"
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
    warnings: tuple[Finding, ...] = ()
    blocking: tuple[Finding, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.blocking


def run_command(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)


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
    return ReviewResult("deterministic", tuple(warnings), tuple(blocking))


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
        return "AI review passed - no blocking findings"
    return f"AI review failed - {len(result.blocking)} blocking finding(s)"


def post_commit_status(
    repo: str,
    sha: str,
    state: str,
    description: str,
    target_url: str = "",
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
        "context=Review Gate",
        "-f",
        f"description={description[:140]}",
    ]
    if target_url:
        args.extend(["-f", f"target_url={target_url}"])
    result = run_command(args)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())


def post_or_update_pr_comment(repo: str, pr_number: int, body: str) -> None:
    comments = run_command(
        ["gh", "api", f"repos/{repo}/issues/{pr_number}/comments", "--paginate"]
    )
    if comments.returncode != 0:
        raise RuntimeError(comments.stderr.strip())
    import json

    comment_id = None
    for comment in json.loads(comments.stdout or "[]"):
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
    args = parser.parse_args()

    review_prompt = os.environ.get("REVIEW_GATE_PROMPT", "").strip()
    result = analyze_workspace(
        Path(args.workspace).resolve(),
        changed_files_for_pr(args.repo, args.pr),
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

    if result.passed and args.submit_approval:
        approved = submit_pr_approval(
            args.repo,
            args.pr,
            f"AI review passed for {args.sha[:8]}. {status_description(result)}",
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
