# Incident Cases

## Payment Webhook Accepted After Signature Mismatch

Policy ID: INC-PAY-001

A prior payment callback bug logged signature mismatches but still accepted refund events. The fix rejected invalid signatures before any state mutation and added replay tests. Similar code should be treated as a merge-blocking payment boundary issue.

Review signal keywords: webhook mismatch accepted, refund accepted, invalid signature, payment incident.

## Payment Token Leaked Through Risk Logs

Policy ID: INC-LOG-001

A previous risk-debug log included card data and a service token. The incident required credential rotation and log scrubbing. Reviewers should block any new log line that prints tokens, full card numbers, webhook signatures, or credentials.

Review signal keywords: token in logs, card in logs, credential exposure, risk check log.

## SQL Interpolation In Velocity Check

Policy ID: INC-SQL-001

A historical velocity-limit query interpolated a user identifier directly into SQL. The fix used parameter binding and added a malicious user id regression test. Similar findings should cite SQL parameterization and payment risk fail-closed policies.

Review signal keywords: velocity limit, user_id SQL, SQL interpolation, malicious user id.
