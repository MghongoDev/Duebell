# Troubleshoot

Find the message you see, then the fix.

## Signing in

| You see | Cause | Fix |
| --- | --- | --- |
| "That email and password do not match." | Wrong details, or the user does not exist. | Run `scripts/reset_demo.py` for the demo user, or [add a user](add-a-user.md). |
| "Too many failed attempts. Wait 15 minutes and try again." | Five wrong passwords for that email. | Wait, or restart the app. |
| Signed out after every restart | `SECRET_KEY` is empty, so each start makes a new one. | Set `SECRET_KEY` in `.env`. |
| Signed in but the dashboard is empty | The records belong to no user. | Run `scripts/create_user.py EMAIL --claim` once. |

## Creating invoices

| You see | Cause | Fix |
| --- | --- | --- |
| "Mock parsing understands..." | Mock mode, or live mode without credentials. | Use the demo sentence, fill the form, or [use a live model](use-a-live-model.md). |
| "Currency must be one of: ..." | The currency is not in `ALLOWED_CURRENCIES`. | Add it, or pick one from the list. |
| "Instalments are only for a High-risk invoice." | The invoice does not score High. | Send it as one invoice. |
| "PayPal rejected the call" with `NOT_AUTHORIZED` | The sandbox app lacks the Invoicing scope. | Turn on Invoicing for the app, or go back to mock mode. |
| "PayPal credentials are missing." | `sandbox` or `toolkit` mode without credentials. | Set `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`. |
| The app stops at start, asking for `requirements-toolkit.txt` | Toolkit mode without the package. | Run `pip install -r requirements-toolkit.txt --no-deps`. |

## Reminders

| You see | Cause | Fix |
| --- | --- | --- |
| **Scan now** drafts nothing | No sent invoice is overdue, or due within `REMINDER_WINDOW_DAYS` with Medium or High risk. Seeded invoices never reached PayPal, so they are skipped. | Create an invoice with short terms, or wait. |
| "This invoice has no PayPal id yet." | The invoice never reached PayPal. | Remind by email, or send the invoice first. |
| "Already sent via PayPal. Approving again sends only the rest." | An earlier approval sent PayPal, then email failed. | Approve again once email works. |
| "PayPal did not confirm the reminder in time..." | A toolkit call timed out. | Check PayPal for the reminder. Duebell counts it as sent. |
| "The email could not be sent" | SMTP refused or timed out. | Check the `SMTP_` settings. The reminder stays pending. |

## Payments and webhooks

| You see | Cause | Fix |
| --- | --- | --- |
| Paid in PayPal, still `SENT` in Duebell | The webhook did not arrive or failed its check. | Click **Refresh** on the row. Check `PAYPAL_WEBHOOK_ID`. |
| Webhook returns `401 webhooks are off in mock mode` | `PAYPAL_MODE=mock` and `MOCK_WEBHOOKS` is off. | For a local demo, set `MOCK_WEBHOOKS=true`. On a public host, leave it off. |
| Webhook returns `400 invalid signature` | Wrong or missing `PAYPAL_WEBHOOK_ID`. | Register again with `scripts/register_webhook.py` and update the ID. |

## Risk comparison

| You see | Cause | Fix |
| --- | --- | --- |
| "Not enough history." | Fewer than 30 invoices with a known outcome, or fewer than 5 late or 5 on time. | Nothing. The panel fills in as payments arrive. |

## Hosting

| You see | Cause | Fix |
| --- | --- | --- |
| Deploy fails with "Set DEMO_PASSWORD." | The start command needs a demo password. | Set `DEMO_PASSWORD` in the Render dashboard. |
| Data gone on the free plan, with or without a deploy | A free service has no persistent disk. Its SQLite file is lost at each deploy, restart and spin-down, which comes after 15 idle minutes. The next start seeds a fresh demo. | Expected on the free plan. For data that lasts, move to a paid instance type, attach a persistent disk, and point `DATABASE_URL` at a file on it. |
| Log at start: "Could not add unique index ux_reminder_pending_invoice" | The database holds two pending reminders for one invoice, left by an older build. | Reject one of the drafts, then restart. |
| Log at start: "Could not add unique index ux_scheduledinvoice_schedule_sequence" | The database holds two scheduled invoices with the same place in one schedule, left by an older build. | Skip the extra one if it is still open, then remove or renumber the duplicate row with `sqlite3`, and restart. The app runs meanwhile, and the scan still claims each item once. |
