# Duebell

Duebell is a bell that rings when an invoice comes due.

It helps freelancers and small businesses get paid. You describe a job in plain English. Duebell sends a PayPal invoice, scores how likely the client is to pay late, and drafts a reminder in a tone that fits. You approve each reminder before it goes out. When the client pays, a PayPal webhook or a refresh records it, and the payment feeds the next score.

![Duebell dashboard](docs/dashboard.png)

## Quick start

You need Python 3.12 or later. Nothing else: the defaults need no PayPal account, model key or email server.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python scripts/reset_demo.py
.venv/bin/uvicorn app.web:app --reload
```

Open http://127.0.0.1:8000 and sign in as `demo@duebell.test` with `duebell-demo-2026`. Then follow [Your first invoice](docs/tutorials/first-invoice.md).

## What it does

- A model turns your sentence into invoice fields. The manual form's checks validate them, and you confirm before anything is sent ([ADR 0004](docs/adr/0004-llm-as-form-filler.md)).
- Each invoice gets a late-payment risk score with reasons you can check ([Risk scores](docs/explanation/risk-scores.md)).
- Duebell drafts reminders for invoices that are overdue or due soon. You approve each one and pick PayPal, email, or both ([Approvals and sending](docs/explanation/approvals-and-sending.md)).
- An invoice can repeat, or a High-risk one can be split into instalments. Every later invoice waits for your approval.
- Signed PayPal webhooks and a manual refresh record payments.
- Each user sees only their own records.

## How it fits together

One FastAPI process over one SQLite file. PayPal sits behind a gateway with three modes: an in-memory mock, the sandbox REST API, and PayPal's Agent Toolkit. The model sits behind one parser function and can only fill a form. A background scheduler drafts reminders and refits the risk comparison; it never sends anything. See [Architecture](docs/explanation/architecture.md).

## Configuration

Three variables pick the modes. All default to `mock`.

| Variable | Values |
| --- | --- |
| `PAYPAL_MODE` | `mock`, `sandbox`, `toolkit` |
| `LLM_MODE` | `mock`, `live` |
| `EMAIL_MODE` | `mock`, `smtp` |

Every other setting has a working default. [Configuration](docs/reference/configuration.md) lists them all. Before hosting, set `SECRET_KEY`, `COOKIE_SECURE=true` and your own `DEMO_PASSWORD`; see [Security](docs/explanation/security.md).

## Status

A PayPal AI Hackathon entry, November 2026.

- The full flow works in mock mode. Tests and the smoke run check it on every change.
- Sandbox and toolkit modes are wired and tested offline. Live sandbox calls returned `403 NOT_AUTHORIZED` for Invoicing during development, so neither has been checked end to end against PayPal.
- The last live parsing eval, on 3 November 2026, got 10 of 10 cases and 50 of 50 fields right.
- The risk weights are set by hand. The training data is synthetic.

## Cost

Mock mode costs nothing. The PayPal sandbox is free and moves no real money. The Render blueprint uses the free plan. Live mode pays your model provider for one short request each time you click **Fill fields with AI**.

## Documentation

[docs/README.md](docs/README.md) is the map. It is split by what you need:

| Folder | For |
| --- | --- |
| [docs/tutorials/](docs/tutorials/) | Learning Duebell by doing. |
| [docs/how-to/](docs/how-to/) | Doing one task: email reminders, sandbox, deploy, users, troubleshooting. |
| [docs/reference/](docs/reference/) | Looking up settings, commands, routes and tables. |
| [docs/explanation/](docs/explanation/) | Understanding the design. |
| [docs/adr/](docs/adr/README.md) | The decision records. |
| [GLOSSARY.md](GLOSSARY.md) | The domain terms. Code and docs use them. |

## Contributing

Issues live on [GitHub](https://github.com/MghongoDev/Duebell/issues). Each piece of work is an issue; the triage labels are in [docs/agents/triage-labels.md](docs/agents/triage-labels.md).

Before you commit, [run the checks](docs/how-to/run-checks.md). Each module docstring cites its issues and decision records. A new domain term goes in the glossary. A decision with a real trade-off gets a new record in `docs/adr/`.

## License

MIT. See [LICENSE](LICENSE).
