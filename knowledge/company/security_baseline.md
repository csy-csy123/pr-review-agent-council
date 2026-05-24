# Security Baseline

## SQL Parameterization

Policy ID: SEC-SQL-001

All production SQL queries must use database-driver parameter binding. Do not build SQL with f-strings, string concatenation, format strings, or template interpolation when request fields, account identifiers, merchant identifiers, or payment identifiers are involved.

Review signal keywords: SQL injection, parameterized query, cursor.execute, f-string SQL, user_id query.

## Command Execution

Policy ID: SEC-CMD-001

Production services must not pass request-derived values into shell commands. Use argument-list subprocess calls with `shell=False`, validate every argument, and avoid shell expansion. Any use of `shell=True` in a request path is security review blocking unless there is a documented exception.

Review signal keywords: shell=True, subprocess, os.system, command injection, request input.

## Secrets And Tokens

Policy ID: SEC-SECRET-001

API tokens, webhook secrets, passwords, private keys, and signing secrets must come from managed secrets or environment variables. They must not be committed to source code, sample fixtures, application logs, CI output, or exception messages.

Review signal keywords: API_TOKEN, token, secret, credential, hardcoded secret.

## Sensitive Logging

Policy ID: SEC-LOG-001

Logs must never include full card numbers, tokens, webhook signatures, passwords, access keys, or payment credentials. Use masked logging helpers and only retain the minimum safe identifier needed for debugging.

Review signal keywords: card number, token log, signature log, sensitive logging, payment logs.
