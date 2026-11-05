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

## Revision, 2026-11-05

Issue #29. The sandbox gateway retries failed calls, so a retry could repeat an action PayPal had already taken. Every POST and PUT now carries a `PayPal-Request-Id` header, one per call, kept the same across that call's retries. The webhook signature check is a POST, so it carries one too. Of the Invoicing calls, PayPal's pages document the header only for creating invoices. PayPal's idempotency guideline describes the header for API requests in general and warns that not every API supports it. Duebell sends it on send, remind and payment calls anyway, because it costs nothing where it is ignored. The live sandbox check in #32 settles which endpoints honour it.
