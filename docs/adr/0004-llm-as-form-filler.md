# 0004 Treat the model as an untrusted form filler

## Status
Accepted

## Date
2026-11-03

## Context
The parse step reads free text from a user and returns invoice fields. That text can contain instructions aimed at the model. The model can also return extra keys, invalid amounts, or an email it invented. The manual form already has validation that the invoice creation path trusts.

## Decision
Ask the model for JSON with a fixed set of keys. Reject any extra key before validation. Run the result through the same `InvoiceProposal` and `validate_proposal` path as the manual form. Keep the system prompt explicit that user text is data, not instructions. In `LLM_MODE=mock`, a narrow deterministic parser handles one canonical demo sentence and refuses anything else. It is a demo aid, not a model, and it is labeled that way. The live call supports an Anthropic-compatible endpoint through `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`, or the usual API key.

## Alternatives considered
- **Trust the model's JSON and coerce it.** Rejected because coercion hides missing fields and can turn an injection attempt into a valid invoice.
- **Let the model call tools or write to the database.** Rejected because parsing is a pure text-to-fields step and does not need agency.
- **A broad local parser that pretends to be the model.** Rejected because it would make the eval look better than the real path. A narrow parser for the demo sentence is acceptable because it refuses everything else and is clearly labeled.

## Consequences
- A prompt injection can change field values, but it cannot add fields or bypass validation.
- The user still reviews the filled form before creating an invoice.
- Live parsing needs a working model endpoint. Mock mode handles the demo sentence and needs the manual form for everything else.
