# Connect the PayPal sandbox

Use this when you want real sandbox invoices instead of the in-memory mock. You need a PayPal developer account with a sandbox REST app.

> PayPal returned `403 NOT_AUTHORIZED` for Invoicing calls during development, even with the Invoicing feature on. If you see it, the app's credentials lack the Invoicing scope. Mock mode keeps working meanwhile.

## Send invoices through the sandbox

1. In the PayPal developer dashboard, open your sandbox app and turn on **Invoicing**.
2. Put these in `.env`:

   ```bash
   PAYPAL_MODE=sandbox
   PAYPAL_CLIENT_ID=your-sandbox-client-id
   PAYPAL_CLIENT_SECRET=your-sandbox-secret
   PAYPAL_INVOICER_EMAIL=your-sandbox-business-email
   ```

3. Restart the app and create an invoice. It appears in the sandbox business account.

Seeded invoices never reached PayPal. Approve reminders only for invoices you create.

## Receive payment webhooks

PayPal needs a public HTTPS URL to call. Without one, use **Refresh** on an invoice row to read its status instead.

1. Expose the app with an HTTPS tunnel of your choice, and note the URL.
2. Register the webhook. The script works only with `PAYPAL_MODE=sandbox`:

   ```bash
   .venv/bin/python scripts/register_webhook.py --url https://your-tunnel.example/webhooks/paypal
   ```

3. Put the printed ID in `.env` as `PAYPAL_WEBHOOK_ID` and restart.

Without `PAYPAL_WEBHOOK_ID`, every webhook fails its signature check and changes nothing.

## Use the Agent Toolkit instead

Toolkit mode sends invoices, sends reminders, records payments and reads status through PayPal's Agent Toolkit. It still creates invoices and checks webhooks through the REST calls, because the toolkit cannot set payment terms or a due date and has no webhook check ([ADR 0006](../adr/0006-agent-toolkit-adapter.md)).

1. Install it without its framework dependencies:

   ```bash
   .venv/bin/pip install -r requirements-toolkit.txt --no-deps
   ```

2. Set `PAYPAL_MODE=toolkit`, keeping the sandbox credentials above.
3. Restart the app.

If the package is missing, the app stops at start with the install command. Toolkit calls do not retry. If one takes longer than `PAYPAL_TIMEOUT_SECONDS`, Duebell treats a reminder as sent and asks you to check PayPal. [Approvals and sending](../explanation/approvals-and-sending.md) explains why.
