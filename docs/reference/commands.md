# Commands

Run each command from the repository root with the virtual environment's Python. Every script reads the same [configuration](configuration.md) as the app, including `DATABASE_URL`.

## Run the app

```bash
.venv/bin/uvicorn app.main:app --reload
```

`app.main` holds the served app; nothing else imports it. The app listens on http://127.0.0.1:8000. The background scheduler drafts reminders every 30 minutes and refits the [risk comparison](../explanation/risk-scores.md) every six hours.

## Data and users

| Command | Does |
| --- | --- |
| `scripts/reset_demo.py` | Empties every table in one transaction and seeds the demo. The same as `scripts/seed.py --reset`. It keeps the database file, so a running app keeps working and sees the new data. It checks `DEMO_PASSWORD` first: if the password is unusable, or unset with `--require-password`, it exits with a one-line error and deletes nothing. |
| `scripts/seed.py` | Creates `demo@duebell.test` if missing, then six clients with set payment habits and 26 invoices, all owned by that user. It writes the clients and invoices together or not at all. On a seeded database it prints "The demo data is already seeded." and changes nothing. Use `--reset` to start over. A `DEMO_PASSWORD` under 10 characters stops it with a one-line error that names the minimum. It then writes no demo data, and with `--reset` it deletes nothing. |
| `scripts/seed.py --if-empty` | Seeds only if the database has no users, so running it at every start keeps existing data. |
| `scripts/seed.py --require-password` | Exits with an error if `DEMO_PASSWORD` is unset, rather than use the published default. |
| `scripts/create_user.py EMAIL` | Adds a user. Reads the password from `DUEBELL_PASSWORD` or a prompt. Passwords need at least 10 characters. |
| `scripts/create_user.py EMAIL --claim` | Also gives that user every record with no owner. If the user already exists, skips creating it and asks for no password. Use it once on a database from before sign-in. |

## Checks

Install the test and lint tools first: `.venv/bin/pip install -r requirements-dev.txt`. See [Run the checks](../how-to/run-checks.md), which also covers updating the dependency lock.

| Command | Does |
| --- | --- |
| `pytest -q` | Runs the test suite. No network, no credentials. Each test builds its app on its own in-memory or temporary database, so a running dev server's database is never touched. |
| `scripts/smoke.py` | Resets a scratch database, `/tmp/duebell-smoke.db`, three times and runs the whole demo through the HTTP routes each time: sign in, parse, create, scan, approve the reminder, simulate payment, replay the webhook, refresh. Prints `Smoke: pass` or the step that failed. |
| `scripts/evals.py` | Ten reminder cases and a webhook replay check, offline. The ten parsing cases run only in live mode with credentials; otherwise the script says it skipped them. Exits non-zero on any failure. |
| `scripts/evals.py --live` | Forces the parsing cases. Fails if live mode has no credentials. |
| `scripts/train_risk.py` | Fits logistic regression on the older three quarters of a synthetic history, by issue date, and prints accuracy and AUC on the newest quarter beside the rule baseline. |

## PayPal

| Command | Does |
| --- | --- |
| `scripts/register_webhook.py --url URL` | Registers a PayPal webhook for invoice events at a public HTTPS URL ending in `/webhooks/paypal`, and prints its ID for `PAYPAL_WEBHOOK_ID`. Needs sandbox credentials. |

## Optional install

```bash
.venv/bin/pip install -r requirements-toolkit.txt --no-deps
```

Installs PayPal's Agent Toolkit for `PAYPAL_MODE=toolkit`. Keep `--no-deps`: the package pins LangChain, CrewAI and OpenAI Agents, which Duebell never imports ([ADR 0006](../adr/0006-agent-toolkit-adapter.md)).
