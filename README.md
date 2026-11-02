# Duebell

Duebell is a bell that rings when an invoice comes due.

It is an invoice-to-cash agent for freelancers and small businesses. You describe a job in plain English. Duebell creates and sends a PayPal sandbox invoice, scores how likely the client is to pay late, and drafts a reminder in the right tone. You approve the reminder before PayPal sends it. When the client pays, a webhook or a refresh closes the loop, and the payment feeds the next score.

This repository is a hackathon entry for the PayPal AI Hackathon. It runs in mock mode by default, so it works with no credentials.

## Status

In progress. Phases are tracked in the issue tracker. The master spec is issue #1.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python scripts/reset_demo.py
.venv/bin/uvicorn app.web:app --reload
```

Open http://127.0.0.1:8000.

## Modes

- `PAYPAL_MODE=mock` uses an in-memory PayPal adapter.
- `PAYPAL_MODE=sandbox` calls the PayPal sandbox REST API.
- `LLM_MODE=mock` disables AI parsing. The manual form and the reminder draft writer still work.
- `LLM_MODE=live` calls an Anthropic-compatible endpoint.

## Evals

The reminder and webhook evals run offline. The parse eval needs a live model.

```bash
.venv/bin/python scripts/evals.py
LLM_MODE=live .venv/bin/python scripts/evals.py
```

## Security and limits

Duebell is a single-user sandbox demo. It has no login, so do not expose it on the public internet.

- PayPal webhook signatures are verified before any state changes.
- Events are deduplicated by PayPal event id.
- Inputs are length-limited, amounts are capped, and currencies are allowlisted.
- Webhook payloads are capped at 1 MB.
- Security headers and a content security policy are set on every response.
- Secrets live in `.env`, which is ignored by git. `.env.example` lists the names.

## Synthetic data

The risk model trains on synthetic client histories. Duebell ships an explainable rule baseline, and a logistic-regression script compares its held-out result beside the baseline. The comparison is a demonstration, not a claim about real clients.

```bash
.venv/bin/python scripts/train_risk.py
```

## Documentation

- `GLOSSARY.md`: domain terms.
- `docs/adr/`: design decisions.
- `docs/architecture.md`: how the pieces fit.
- `docs/submission.md`: hackathon submission notes.
- `docs/demo-script.md`: the recording plan.

## License

MIT. See `LICENSE`.
