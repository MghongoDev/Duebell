# 0001 Use a gateway boundary with mock and sandbox adapters

## Status
Accepted

## Date
2026-11-03

## Context
The demo must run with no credentials, and the same flows must still exercise the real PayPal sandbox. Tests need to inspect what the app asked PayPal to do without making network calls. The PayPal Invoicing v2 API also has a habit of changing status codes and response shapes between sandbox and production.

## Decision
Define `PayPalGateway` as a Python `Protocol` in `app/paypal.py`. `MockPayPal` keeps invoices and service calls in memory. `SandboxPayPal` calls the REST API and caches the OAuth token. `build_gateway(settings)` picks one from `PAYPAL_MODE`, and mock is the default.

## Alternatives considered
- **Call the REST API directly from services.** Rejected because every test would need HTTP interception, and the demo would fail without credentials.
- **Use the official PayPal SDK.** Rejected because the SDK pulls a large dependency surface for five endpoints, and the REST payloads are small enough to keep explicit.
- **A single adapter with a mock switch inside.** Rejected because the switch would spread through the code and the mock would still carry HTTP imports.

## Consequences
- Services depend on one small interface, not on httpx or PayPal.
- Tests assert against `MockPayPal.sent_reminders` and invoice state.
- The sandbox adapter is the only file that knows PayPal paths and payloads.
- A sandbox-only bug can still hide from the test suite. The live checkpoint covers that gap.
