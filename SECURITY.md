# Security Policy

## Reporting a Vulnerability

Please do not open a public issue containing credentials, private notification endpoints,
wallet-identifying operational logs, or other sensitive data. Report security concerns
privately to [hessalex02@gmail.com](mailto:hessalex02@gmail.com).

## Credential Handling

Notification credentials must be supplied through runtime environment variables and must never be
committed or passed as command-line arguments. Command-line secrets can be exposed through shell
history and process listings. Generated audit files and reports are excluded from Git.

Signal Scout accepts only HTTPS endpoints for outbound notifications and market-data requests.
Live mode refuses the insecure certificate-verification override. Audit records may contain public
wallet identifiers and should still be treated as operational data rather than published blindly.

Signal Scout consumes public market information and does not place trades or manage funds.
