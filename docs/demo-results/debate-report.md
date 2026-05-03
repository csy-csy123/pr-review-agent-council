# Demo Result: Debate Council Report

This is a sanitized demo report generated from the built-in `payment_risk.py` PR fixture.

- Mode: `debate`
- Verdict: `request_changes`
- Raw candidate findings: 21
- Accepted findings: 19
- Standardized report accepted findings: 11
- Standardized report rejected findings: 2
- Standardized report downgraded findings: 1

The evidence snippets below come from demo code only. Token-like values are represented as placeholders.

## Summary

The debate mode keeps the same specialist reviewer coverage as council mode, but adds a dynamic Lead Debate Controller. The controller can ask the critic to challenge a finding, request more evidence, ask the original reviewer to defend or revise, merge duplicates, and accept or reject findings.

Compared with the council baseline, the debate report is stronger on evidence quality, severity calibration, and duplicate/noise control.

## Representative Findings

### 1. [P1] SQL injection in `fetch_recent_payment_count`

- File: `demo/pr-fixture/payment_risk.py`
- Evidence: user-controlled `user_id` is interpolated directly into an SQL query.
- Impact: an attacker may inject SQL and access or modify payment data.
- Fix: use a parameterized query and pass `user_id` as a bound parameter.
- Why accepted: multiple reviewers found the same root issue, and the debate loop consolidated evidence into a single high-confidence security finding.

### 2. [P1] Sensitive token exposure in logging

- File: `demo/pr-fixture/payment_risk.py`
- Evidence: a log line prints a token variable together with request details.
- Impact: anyone with log access may see sensitive credential material.
- Fix: remove the token from logs and mask sensitive fields.
- Why accepted: the evidence is concrete and directly tied to a changed code path.

### 3. [P1] Unsafe shell execution surface

- File: `demo/pr-fixture/payment_risk.py`
- Evidence: `subprocess.run(..., shell=True)` is used with interpolated payment values.
- Impact: request-derived values can reach a shell execution surface.
- Fix: use an argument list, keep `shell=False`, and validate input before execution.
- Why accepted: the debate loop kept the issue because the risky execution surface is explicit and actionable.

### 4. [P1] Insecure webhook signature handling

- File: `demo/pr-fixture/payment_risk.py`
- Evidence: webhook signature comparison and replay handling are insufficient for a payment callback path.
- Impact: forged or replayed webhook events may be accepted.
- Fix: use constant-time comparison, reject invalid signatures, and add replay protection.
- Why accepted: the issue affects a security-sensitive boundary and has clear remediation.

### 5. [P2] Swallowed exceptions and missing test coverage

- File: `demo/pr-fixture/payment_risk.py`
- Evidence: exceptions are swallowed, and risk-control logic lacks focused tests.
- Impact: failures can disappear silently, and fraud-control regressions may not be caught.
- Fix: handle specific exceptions, log safe context, re-raise where appropriate, and add tests for key risk decisions.
- Why accepted: the finding is actionable but less severe than direct injection or credential exposure.

## Judge Summary

The AI Judge scored debate mode at **92/100**.

The largest improvement is duplicate/noise control: council scored **40**, while debate scored **98**. Evidence quality improved from **85** to **95**, and severity accuracy improved from **70** to **90**.
