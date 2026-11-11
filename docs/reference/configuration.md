# Configuration

Duebell reads its settings from environment variables when it starts. It also loads `.env` from the working directory, the folder you run the command from. It does not look in parent folders, so run commands from the repository root. Variables already set in the environment win over `.env`. A relative `DATABASE_URL`, such as the default, is relative to the same folder. `.env.example` lists every name, and git ignores `.env`.

The defaults run the whole demo with no credentials. A blank value, such as `ANTHROPIC_MODEL=` in `.env.example`, counts as unset, so the default applies.

On/off settings (`MOCK_WEBHOOKS`, `SMTP_STARTTLS`, `COOKIE_SECURE`) accept `on`, `yes`, `true` or `1` for on, and `off`, `no`, `false` or `0` for off. Case and surrounding spaces do not matter. Any other value stops Duebell at start with an error that names the setting.

## Modes

| Variable | Default | Values | Effect |
| --- | --- | --- | --- |
| `PAYPAL_MODE` | `mock` | `mock`, `sandbox`, `toolkit` | Picks the [gateway](../../GLOSSARY.md). `mock` keeps invoices in memory. `sandbox` calls the PayPal sandbox REST API. `toolkit` sends, reminds, records payments and reads status through PayPal's Agent Toolkit ([ADR 0006](../adr/0006-agent-toolkit-adapter.md)). |
| `LLM_MODE` | `mock` | `mock`, `live` | `mock` parses only the demo sentence. `live` calls an Anthropic-compatible model. |
| `EMAIL_MODE` | `mock` | `mock`, `smtp` | `mock` keeps reminder emails in memory. `smtp` sends them. |
| `MOCK_WEBHOOKS` | `false` | on/off | Mock mode cannot check a webhook's signature, so it refuses every webhook with `401` and stores nothing unless this is on. `.env.example` turns it on for the local demo. Leave it off on a public host; `render.yaml` does. It has no effect in `sandbox` or `toolkit` mode, which always check the signature through PayPal. |

A `*_MODE` value other than those listed falls back to `mock`.

## PayPal

| Variable | Default | Notes |
| --- | --- | --- |
| `PAYPAL_CLIENT_ID` | empty | Needed in `sandbox` and `toolkit` modes. |
| `PAYPAL_CLIENT_SECRET` | empty | Needed in `sandbox` and `toolkit` modes. |
| `PAYPAL_WEBHOOK_ID` | empty | Without it, every webhook fails the signature check. `scripts/register_webhook.py` prints one. |
| `PAYPAL_INVOICER_EMAIL` | empty | The merchant email on each invoice. |
| `PAYPAL_BASE_URL` | `https://api-m.sandbox.paypal.com` | Toolkit mode uses the sandbox only when this URL contains `sandbox`. |
| `PAYPAL_TIMEOUT_SECONDS` | `20` | How long one PayPal call may take. |

## Model

| Variable | Default | Notes |
| --- | --- | --- |
| `ANTHROPIC_BASE_URL` | empty | Live mode needs it. |
| `ANTHROPIC_API_KEY` | empty | Live mode needs this or `ANTHROPIC_AUTH_TOKEN`. |
| `ANTHROPIC_AUTH_TOKEN` | empty | An alternative to the API key. |
| `ANTHROPIC_MODEL` | `claude-haiku-5-5` | The model that fills the form. |
| `LLM_TIMEOUT_SECONDS` | `30` | How long one model call may take. |

If live mode lacks a credential or a base URL, parsing falls back to the mock parser.

## Email

| Variable | Default | Notes |
| --- | --- | --- |
| `SMTP_HOST` | empty | `smtp` mode needs it. |
| `SMTP_PORT` | `587` | |
| `SMTP_USERNAME` | empty | Leave empty to skip login. |
| `SMTP_PASSWORD` | empty | |
| `SMTP_FROM` | empty | `smtp` mode needs it. |
| `SMTP_STARTTLS` | `true` | On/off. Turn it off only for a server that does not offer STARTTLS. |
| `SMTP_TIMEOUT_SECONDS` | `20` | |

## Sign-in

| Variable | Default | Notes |
| --- | --- | --- |
| `SECRET_KEY` | empty | Signs the session cookie. If empty, Duebell makes a random key at start, and every session ends when the process restarts. |
| `COOKIE_SECURE` | `false` | On/off. Turn it on behind HTTPS so browsers send the cookie only over HTTPS. |
| `DEMO_PASSWORD` | empty | The password the seed gives `demo@duebell.test`. If empty, the seed uses `duebell-demo-2026`, unless it runs with `--require-password`. |
| `DUEBELL_PASSWORD` | empty | Read only by `scripts/create_user.py`, instead of prompting. |

## Invoices and reminders

| Variable | Default | Notes |
| --- | --- | --- |
| `ALLOWED_CURRENCIES` | `USD` | Comma-separated, for example `USD,EUR`. The first is the form default. |
| `AMOUNT_CAP_CENTS` | `1000000` | The largest invoice amount, in the currency's minor units: cents for USD, yen for JPY. The same number applies to every currency. |
| `TERMS_MIN_DAYS` | `0` | |
| `TERMS_MAX_DAYS` | `120` | |
| `REMINDER_WINDOW_DAYS` | `3` | How many days before the due date the scan may draft a reminder for a Medium or High risk invoice. |

## Storage

| Variable | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./duebell.db` | A SQLAlchemy URL. Duebell runs as one process over SQLite ([ADR 0005](../adr/0005-sqlite-and-payment-sync.md)). |

## Fixed limits

These are constants in the code, not settings.

| Limit | Value |
| --- | --- |
| Client name | 120 characters |
| Work description | 500 characters |
| Text sent to the AI parser | 2,000 characters |
| Reminder draft | 2,000 characters |
| Webhook body | 1 MB |
| Instalments | 2 to 6, High-risk invoices only |
| Repeats | 1 to 60, or no end |
| Session length | 12 hours |
| Sign-in attempts before a lockout | 5 within 15 minutes, counted before the password check. A correct password clears the count. |
| Emails the lockout tracks | 10,000. Entries expire after 15 minutes. |
| Retries for sandbox PayPal calls and model calls | 3 attempts, exponential backoff. Toolkit calls do not retry. |
