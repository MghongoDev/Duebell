# 0006 Use the PayPal Agent Toolkit as a gateway adapter, not as model tools

## Status

Accepted

## Date

2026-11-05

## Context

PayPal's Agent Toolkit wraps the Invoicing API as tools for agent frameworks. Using it targets the "Best Use of PayPal + AI" category. The obvious way to use it is to give the model the tools and let it create, send and remind. That conflicts with ADR 0004, which keeps the model to filling a form, and ADR 0003, which says nothing reaches a client without a person's approval.

We read version 1.11.0 before building on it, and found:

- Its create and update tools cannot set payment terms or a due date. Duebell's reminders all run off the due date.
- It has no tool for webhook signature verification.
- Its HTTP calls set no timeout and do not retry. It fetches a new access token for every call.
- The reminder tool prints the request URL and PayPal's response to stdout.
- The package pins LangChain, CrewAI and OpenAI Agents. Its shared invoice layer, `PayPalAPI.run`, needs only pydantic, httpx and requests.

## Decision

Add `ToolkitPayPal` as a third adapter behind `PayPalGateway`, chosen by `PAYPAL_MODE=toolkit`. The app calls the toolkit directly through `PayPalAPI.run`. The model never sees the tools.

- Send, get status, send reminder and record payment go through the toolkit.
- Create and webhook verification stay on the REST code inherited from `SandboxPayPal`, so terms, due dates and signature checks behave as in sandbox mode.
- Each toolkit call runs under `PAYPAL_TIMEOUT_SECONDS` in a worker thread. Toolkit errors become `PayPalError` with a short message.
- The toolkit is an optional install: `pip install -r requirements-toolkit.txt --no-deps`. It is not in `requirements.txt`.

## Alternatives considered

- **Give the model the toolkit's tools.** Rejected because it removes the approval step and lets prompt text decide which PayPal action runs. Changing that would mean replacing ADRs 0003 and 0004 first.
- **Use the toolkit for every call, create included.** Rejected because the client would see PayPal's default terms while Duebell tracks a different due date.
- **Install the toolkit with its dependencies.** Rejected because the pinned frameworks add a large dependency surface for code Duebell never imports, the same reason ADR 0001 gives for not using the SDK.

## Consequences

- Toolkit mode runs the whole demo, and tests cover it with a fake toolkit and no network.
- Toolkit calls do not retry. A failed send or reminder shows an error and the operator tries again.
- A toolkit call that times out keeps running in its worker thread until `requests` gives up. The app has already answered the operator.
- The reminder tool's stdout lines show up in the server log.
- The toolkit sends `null` for unused optional fields. Only a live sandbox run can confirm PayPal accepts them. That check waits until the sandbox app's Invoicing scope stops returning 403.
