---
id: severity-gate
name: No open Critical, Major, or security finding
mode: error
report: platform
---

<!-- Source: dmc-technologies/review-rules rules/coderabbit/pre-merge-checks/severity-gate.md (MODEV-1232). Change it there. -->

Decide this check only from CodeRabbit's own review comments and the new
findings from this review. Do not search the code for new problems.

Fail if any CodeRabbit review comment or new finding is rated Critical or
Major, or is one that CodeRabbit classes as a security finding, and does not
have a confirmed code fix (code=addressed). A committed secret or a result
from CodeRabbit's gitleaks, trufflehog, semgrep, or zizmor tools counts only
when CodeRabbit reports it as a security finding. A new finding from the
current review has no code status and counts as not fixed. A resolved
discussion alone does not count as fixed. If the code may have changed since
a comment was assessed, inspect the current code only to decide whether that
finding is fixed.

Ignore every other Minor, Trivial, and Info comment. They never fail this
check.

On every result, pass or fail, state the head commit whose CodeRabbit review
it read, how many CodeRabbit review comments and new findings it examined,
and how many of them are open Critical, Major, or security findings. If no
CodeRabbit review of the head commit exists, or its review comments cannot be
loaded, report the check as inconclusive, never as passed.

This check is the findings gate. The fail_commit_status setting only fails
a CodeRabbit review that stops because of an error; it never fails on
findings.

When this check fails because of a security finding, begin the explanation
with HUMAN STOP: a dmc-developers member resolves it before merge. For every
failure, name the file and line of each open finding.
