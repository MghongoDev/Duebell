# Send reminders by email

PayPal's own reminder is the default channel. To email the client as well, or instead, give Duebell an SMTP server.

1. Put these in `.env`:

   ```bash
   EMAIL_MODE=smtp
   SMTP_HOST=smtp.example.com
   SMTP_PORT=587
   SMTP_USERNAME=you@example.com
   SMTP_PASSWORD=your-password
   SMTP_FROM=billing@example.com
   ```

   STARTTLS is on by default. Set `SMTP_STARTTLS=false` only for a server that does not support it.

2. Restart the app.
3. On a pending reminder, pick **Email** or **PayPal and email** in the list beside **Approve and send**, then approve.

The email goes to the client's address on the invoice, with the subject "Reminder: invoice" and the invoice number. The body is the draft you approved.

In mock mode, the default, emails stay in memory and nobody receives them.

## When a send fails

If PayPal succeeds and the email fails, the reminder stays pending and the page shows the error. The draft notes that PayPal already sent it. Approve again and only the email goes out.

An invoice without a PayPal ID can still be reminded by email alone. The scan drafts reminders only for invoices PayPal holds, so this applies only to older drafts.
