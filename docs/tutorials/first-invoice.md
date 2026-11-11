# Your first invoice, from sentence to payment

This tutorial runs Duebell on your machine and takes one invoice through its whole life: you describe it, send it, remind the client, and record the payment. It uses mock mode, so you need no PayPal account, model key or email server. It takes about ten minutes.

You need Python 3.12 or later and a terminal. Run every command from the repository root.

## 1. Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

The copied `.env` leaves every mode on `mock`.

## 2. Load the demo data

```bash
.venv/bin/python scripts/reset_demo.py
```

The script empties any old database and creates six clients with different payment habits. It prints the sign-in details:

```text
Emptied every table in place. A running app keeps working and sees the new data.
Seeded 6 clients and 26 invoices.
Sign in as demo@duebell.test. The password is DEMO_PASSWORD, or duebell-demo-2026 if unset.
```

## 3. Start the app and sign in

```bash
.venv/bin/uvicorn app.web:app --reload
```

Open http://127.0.0.1:8000. You land on the sign-in page. Sign in as `demo@duebell.test` with `duebell-demo-2026`.

The dashboard shows totals, the seeded invoices with their risk badges, and empty panels for reminders and scheduled invoices.

## 4. Describe an invoice

In **Describe the invoice**, paste:

```text
Invoice Acme Studio, billing@acme.test, 1200 USD for logo design, net 2 days.
```

Click **Fill fields with AI**. The form fills in. Nothing is created yet: the parser only fills the form, and you decide what happens next.

In mock mode the parser understands only this sentence pattern. Anything else gets a message asking you to fill the form by hand.

Click **Create invoice**. Mock PayPal creates and sends it, and a new row appears with status `SENT`.

## 5. Read the risk

Look at the new row's risk badge. Acme Studio is seeded as a client who pays about nine days late, so this invoice scores **High**. The reasons under the badge say why, in plain words.

## 6. Remind the client

In **Reminders**, click **Scan now**. One draft appears for your new invoice. It is due in two days and scores High, so it falls inside the reminder window. The draft quotes the invoice number, amount and due date, in a firm tone because of the risk.

Nothing has been sent. Change the text if you like and click **Save draft**. Leave the channel list beside the button on **PayPal** and click **Approve and send**. The reminder's status becomes `SENT`, and the activity feed records it.

## 7. Record the payment

In the invoice's row, click **Simulate payment**. The status becomes `PAID`, the Collected total rises, and Acme Studio's next invoice will count this payment in its history.

## What you did

You took one invoice through every step Duebell manages: parse, validate, send, score, draft a reminder, approve it, and record payment. Each step that reaches the client waited for your click.

## If something goes wrong

If you cannot sign in, run step 2 again in a second terminal: it recreates the demo user. Leave the app running; it picks up the new data. If the page says "Too many failed attempts", wait 15 minutes or restart the app. If **Scan now** drafts nothing, check the sentence says `net 2 days`. Every other error message is in [Troubleshoot](../how-to/troubleshoot.md).

## Next

- [Repeat or split an invoice](../how-to/repeat-or-split-an-invoice.md)
- [Send reminders by email](../how-to/send-reminders-by-email.md)
- [How approvals work](../explanation/approvals-and-sending.md)
