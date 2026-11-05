# Repeat or split an invoice

An invoice can repeat, or you can split a High-risk one into instalments, but not both. Every invoice after the first waits in **Scheduled invoices** until you approve it ([ADR 0007](../adr/0007-scheduled-invoices-need-approval.md)).

## Repeat an invoice

1. Fill the new invoice form as usual.
2. Set **Repeat** to **Weekly** or **Monthly**.
3. Optionally set **Repeats** to how many more invoices to send, from 1 to 60. Leave it blank for no end.
4. Click **Create invoice**.

The first invoice goes out at once. The next one waits until its date. The scan then moves it to **Scheduled invoices**, where you click **Approve and send** or **Skip this one**. Skipping one leaves the repeat running.

Monthly repeats keep the day of the month. When a month is shorter, the invoice moves to its last day.

## Split an invoice into instalments

The form offers instalments only when the invoice scores High risk. The offer appears when you change the client email, amount, currency or terms, or after **Fill fields with AI**.

1. Fill the form for the client.
2. When the instalment box appears, choose 2 to 6 instalments and monthly or weekly spacing.
3. Click **Create invoice**.

Nothing goes to PayPal yet. The first instalment appears in **Scheduled invoices** at once, and the rest follow on their dates. The amounts differ by at most one cent and add up to the exact total. Each approved instalment is a normal invoice with its own reminders.

The server refuses a split for an invoice that is not High risk.

## Stop a schedule

Click **Cancel** next to it under **Scheduled invoices**. Duebell cancels every upcoming or pending invoice from it and leaves sent invoices alone. A schedule stays listed, and can be cancelled, until its last invoice is approved or skipped.

## If approval fails

If PayPal fails while sending, the scheduled invoice stays pending and the page shows the error. Approve it again. When PayPal had already created the invoice, Duebell sends that one instead of creating another.
