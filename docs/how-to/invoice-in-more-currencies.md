# Invoice in more than one currency

1. List the currencies in `.env`, the default first:

   ```bash
   ALLOWED_CURRENCIES=USD,EUR,GBP
   ```

2. Restart the app. The form's **Currency** list now offers them.

PayPal receives the invoice in its own currency. Duebell never converts amounts:

- Totals show one figure per currency, for example `1,200.00 USD · 500.00 EUR`.
- The risk score compares an amount only with the client's invoices in the same currency. A client's first invoice in a new currency gets a limited-data label. See [Risk scores](../explanation/risk-scores.md).

## Currencies without decimals

PayPal takes HUF, JPY and TWD only as whole numbers ([PayPal currency codes](https://developer.paypal.com/api/rest/reference/currency-codes/)). For these currencies Duebell:

- shows and sends amounts with no decimals, for example `1,000 JPY`;
- refuses an amount with decimals on the form, with the message `JPY amounts are whole numbers. Remove the decimals.`

All other currencies use two decimals.

Duebell stores each amount in the currency's minor unit, so `AMOUNT_CAP_CENTS=1000000` caps a USD invoice at 10,000.00 and a JPY invoice at 1,000,000. See [Configuration](../reference/configuration.md).

## Which currencies PayPal accepts

PayPal decides which currencies your account can invoice in. A currency on the list that PayPal refuses fails when you create the invoice.
