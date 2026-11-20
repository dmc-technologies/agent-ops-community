---
id: severity-gate
name: No open Critical, Major, or secret finding
mode: error
report: platform
---

<!-- Source: dmc-technologies/review-rules rules/coderabbit/pre-merge-checks/severity-gate.md (MODEV-1232). Change it there. -->

Read CodeRabbit's review comments and the new findings from this review.

Fail if any CodeRabbit review comment or new finding with Critical or Major
severity does not have a confirmed code fix (code=addressed). A new finding
from the current review has no code status and counts as not fixed. A
resolved discussion alone does not count as fixed. If the code may have
changed since a comment was assessed, inspect the current code. This
includes every security finding, from the review or from a scanner such as
gitleaks, trufflehog, semgrep, or zizmor, that is rated Critical or Major.

Fail if the changed lines add a credential, private key, access token, or
other secret, including a value that looks like an example or placeholder.
A person decides whether it is real; this check never assumes it is fake.

Ignore Minor, Trivial, and Info comments. They never fail this check.

This check is the findings gate. The fail_commit_status setting only fails
a CodeRabbit review that stops because of an error; it never fails on
findings.

When this check fails because of a secret or a security finding, begin the
explanation with HUMAN STOP: a dmc-developers member resolves it before
merge. For every failure, name the file and line of each open finding.
