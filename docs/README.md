# Duebell documentation

Pick the line that matches what you want.

## Learn

- [Your first invoice, from sentence to payment](tutorials/first-invoice.md). Start here. Ten minutes, no accounts.

## Do a task

| I want to | Guide |
| --- | --- |
| Repeat an invoice, or split it into instalments | [Repeat or split an invoice](how-to/repeat-or-split-an-invoice.md) |
| Remind clients by email | [Send reminders by email](how-to/send-reminders-by-email.md) |
| Invoice in EUR or another currency | [Invoice in more than one currency](how-to/invoice-in-more-currencies.md) |
| Give someone an account | [Add a user](how-to/add-a-user.md) |
| Use real PayPal sandbox invoices or the Agent Toolkit | [Connect the PayPal sandbox](how-to/connect-paypal-sandbox.md) |
| Parse any description with a real model | [Parse free text with a live model](how-to/use-a-live-model.md) |
| Put it online | [Deploy to Render](how-to/deploy-to-render.md) |
| Check a change before committing | [Run the checks](how-to/run-checks.md) |
| Fix an error message | [Troubleshoot](how-to/troubleshoot.md) |

## Look something up

- [Configuration](reference/configuration.md): every environment variable and fixed limit.
- [Commands](reference/commands.md): every script and its options.
- [HTTP routes](reference/http-routes.md): every route, its fields and its errors.
- [Data model](reference/data-model.md): tables and statuses.
- [Glossary](../GLOSSARY.md): what each domain term means.

## Understand why

- [Architecture](explanation/architecture.md): the parts and how a request moves through them.
- [Approvals and sending](explanation/approvals-and-sending.md): why nothing goes out without a click, and how Duebell avoids sending twice.
- [Risk scores](explanation/risk-scores.md): the rule baseline, the risk comparison, and currencies.
- [Security](explanation/security.md): sign-in, ownership, limits and known gaps.
- [Decision records](adr/README.md): each design decision and the options rejected.

## Hackathon

- [Submission notes](submission.md)
- [Demo video script](demo-script.md)

## Keeping these pages right

Each page has one job: teach, guide, describe or explain. Put a new fact on the page whose job it fits, and link to it from the others rather than repeat it. When code changes a variable, route, command or status, update the matching reference page in the same commit.
