# Demo script

Target length: 2 minutes 45 seconds. Record at 1440p or higher. Keep the cursor deliberate.

## Before recording

```bash
.venv/bin/python scripts/reset_demo.py
PAYPAL_MODE=mock LLM_MODE=live .venv/bin/uvicorn app.main:app
```

Sign in as `demo@duebell.test` (password `duebell-demo-2026`, or your `DEMO_PASSWORD`) before recording, so the video opens on the dashboard.

Have the sentence ready: `Invoice Acme Studio, billing@acme.test, 1200 USD for logo design, net 2 days.`

If the model endpoint is unavailable, fill the form manually and say that the manual path is the fallback. Do not hide the failure.

## Script

### 0:00 to 0:20: The problem

Show the dashboard. Pick "Overdue" in the status filter to show the late invoices, then set it back to "All statuses".

> Freelancers send the invoice and then have to chase it. The chase is awkward, easy to postpone, and easy to get wrong. Duebell handles the follow-up, but never sends a reminder without you.

### 0:20 to 0:50: Describe the invoice

Paste the sentence into "Describe the invoice". Click "Fill fields with AI".

> The model fills the form. It does not create the invoice. The same validation as the manual form checks every field, and unexpected fields are rejected.

Point at the filled fields. Click "Create invoice".

### 0:50 to 1:20: Risk

Show the new row and its risk badge and reasons.

> The risk score is a rule baseline. It looks at how often this client paid late, how late, the size of this invoice, and the terms. Every score comes with reasons. A new client gets a limited-data label instead of a confident score.

### 1:20 to 1:50: Reminder and approval

Click "Scan now". Show the pending reminder.

> The scan only drafts. Here is the draft, built from the invoice number, the amount, and the due date. Nothing has been sent.

Click "Approve and send".

> Approval is the only path to PayPal. The scheduler can run all day and still cannot send by itself.

### 1:50 to 2:15: Payment sync

Click "Simulate payment" on the new row. Show the status change to `PAID` and the "Simulated payment" line at the top of the activity feed. The seeded invoices have no PayPal ID, so their rows show no "Simulate payment" or "Refresh".

> In sandbox, a signed webhook does this. Events are stored once by PayPal's event id, so a replay changes nothing. A manual refresh covers a missed webhook.

### 2:15 to 2:40: How it fits together

Show `docs/explanation/architecture.md` or a simple diagram.

> One FastAPI process, one SQLite file, one gateway boundary. Mock, sandbox and toolkit gateways share the same interface. The model is an untrusted form filler. The risk score is explainable. The PayPal webhook is signed and idempotent.

### 2:40 to 2:45: Close

> Duebell turns a late invoice into a calm, approved follow-up. The repo and setup steps are in the README.

## After recording

- Trim dead air.
- Add captions for the key sentences.
- Confirm the video is under 3 minutes and public.
- Add the link to `docs/submission.md` and the README.
