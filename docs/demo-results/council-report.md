# Demo Result: Council Baseline Report

This is a sanitized demo report generated from the built-in `payment_risk.py` PR fixture.

- Mode: `council`
- Verdict: `request_changes`
- Raw candidate findings: 19
- Accepted findings: 16
- Standardized report accepted findings: 12
- Standardized report rejected findings: 1
- Standardized report downgraded findings: 4

The evidence snippets below come from demo code only. Token-like values are represented as placeholders.

## Summary

The council baseline finds the major risk areas in the demo PR, including SQL injection, mutable default arguments, unsafe command execution, weak webhook handling, swallowed exceptions, and missing tests.

Its main weakness is duplicate/noise control. Multiple reviewers can report the same underlying issue independently, so the final result contains repeated SQL injection and mutable default argument findings.

## Representative Findings

### 1. [P1] SQL injection in `fetch_recent_payment_count`

- File: `demo/pr-fixture/payment_risk.py`
- Evidence: user-controlled `user_id` is interpolated into an SQL query with an f-string.
- Impact: an attacker may inject SQL and read or modify payment data.
- Fix: use parameterized queries.
- Status: accepted.

### 2. [P1] Mutable default argument in payment scoring

- File: `demo/pr-fixture/payment_risk.py`
- Evidence: a function uses a mutable list or dict as a default argument.
- Impact: state can leak between requests and cause incorrect scoring behavior.
- Fix: default to `None` and initialize the list or dict inside the function.
- Status: accepted.

### 3. [P2] Shell command built from request-derived values

- File: `demo/pr-fixture/payment_risk.py`
- Evidence: `subprocess.run(..., shell=True)` is used with interpolated values.
- Impact: request-derived input can reach a shell execution surface.
- Fix: use an argument list, validate input, and avoid `shell=True`.
- Status: downgraded in the standardized report because the exploit path needs clearer proof.

### 4. [P2] Missing tests for risk controls

- File: `demo/pr-fixture/payment_risk.py`
- Evidence: velocity-limit and high-risk-country logic changed without nearby tests.
- Impact: fraud-control regressions may ship without automated detection.
- Fix: add tests for velocity limit, high-risk country behavior, webhook rejection, and error paths.
- Status: partially accepted.

## Judge Summary

The AI Judge scored this baseline at **72/100**.

The strongest part is critical issue coverage. The weakest part is duplicate/noise control, because several findings describe the same SQL injection or mutable default argument issue.
