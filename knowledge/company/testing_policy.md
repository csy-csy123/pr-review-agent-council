# Testing Policy

## Security And Payment Changes

Policy ID: TEST-PAY-001

Any change touching payment approval, refund handling, webhook validation, secrets, logging, SQL access, or command execution must include targeted automated tests or a documented test exemption. Tests should cover both success and failure paths.

Review signal keywords: payment, webhook, refund, SQL, shell, token, no tests changed.

## Failure Path Coverage

Policy ID: TEST-FAIL-001

Risk-control code must include tests for database failures, malformed payloads, signature mismatch, replayed events, and boundary values. Silent exception handling must be covered by tests that prove the system fails closed.

Review signal keywords: exception path, malformed payload, signature mismatch, replayed event, fail closed.
