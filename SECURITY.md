# Security Policy

## Reporting a Vulnerability

Please do not open a public issue containing credentials, private notification endpoints,
wallet-identifying operational logs, or other sensitive data. Report security concerns
privately to [hessalex02@gmail.com](mailto:hessalex02@gmail.com).

## Credential Handling

Notification credentials must be passed through runtime environment variables or command-line
arguments and must never be committed. Generated audit files and reports are excluded from Git.

Signal Scout consumes public market information and does not place trades or manage funds.
