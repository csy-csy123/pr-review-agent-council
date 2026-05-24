# Payment Review Policy

## Webhook Signature Verification

Policy ID: PAY-WEBHOOK-001

Payment webhooks must verify signatures before mutating state or accepting events. Signature mismatch must fail closed with an explicit rejection response. Use constant-time comparison for signature material and do not continue into refund, settlement, or ledger logic after a failed signature check.

Review signal keywords: webhook signature, X-Signature, mismatch, accepted, reject, fail closed.

## Webhook Replay And Idempotency

Policy ID: PAY-WEBHOOK-002

Every payment callback event must be idempotent. Event identifiers must be persisted in a shared store with a retention window. In-memory lists or mutable defaults are not acceptable replay protection for production callbacks.

Review signal keywords: processed_events, duplicate webhook, replay, idempotency, mutable default.

## Risk Controls Fail Closed

Policy ID: PAY-RISK-001

Payment risk checks must fail closed. Database errors, parser errors, or downstream risk-service failures must not silently default to approval or a zero-risk value. Exceptions require explicit handling, safe logging, and a conservative decision such as manual review.

Review signal keywords: risk check, exception pass, default allow, manual_review, velocity limit.

## High Risk Country Handling

Policy ID: PAY-RISK-002

High-risk country rules must not directly approve payment attempts solely because the amount is small. Low amount can reduce severity, but the decision must still consider merchant trust, velocity, user history, and compliance review requirements.

Review signal keywords: high risk country, low amount, approved, compliance, risk rule.
