# Invoice in more than one currency

1. List the currencies in `.env`, the default first:

   ```bash
   ALLOWED_CURRENCIES=USD,EUR,GBP
   ```

2. Restart the app. The form's **Currency** list now offers them.

PayPal receives the invoice in its own currency. Duebell never converts amounts:

- Totals show one figure per currency, for example `1,200.00 USD · 500.00 EUR`.
- The risk score compares an amount only with the client's invoices in the same currency. A client's first invoice in a new currency gets a limited-data label. See [Risk scores](../explanation/risk-scores.md).

PayPal decides which currencies your account can invoice in. A currency on the list that PayPal refuses fails when you create the invoice.
