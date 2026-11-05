# Decision records

Each record states one decision, the options rejected, and what follows from it. Read the ones that touch the area you are about to change. A new record takes the next number. An old record is not rewritten; a later revision note or record supersedes it.

| Record | Decision |
| --- | --- |
| [0001](0001-paypal-gateway-boundary.md) | Reach PayPal through one gateway, with a mock for offline use. |
| [0002](0002-rule-baseline-risk.md) | Ship a rule baseline with reasons; keep any trained model as a comparison. |
| [0003](0003-human-approval.md) | A person approves every reminder before it is sent. |
| [0004](0004-llm-as-form-filler.md) | The model fills a form and nothing else. |
| [0005](0005-sqlite-and-payment-sync.md) | One process over SQLite; a signed webhook plus a manual refresh. |
| [0006](0006-agent-toolkit-adapter.md) | Use PayPal's Agent Toolkit as a gateway, not as model tools. |
| [0007](0007-scheduled-invoices-need-approval.md) | Repeats and instalments wait for approval, like reminders. |
| [0008](0008-sign-in-and-ownership.md) | Sign in with a signed cookie; every record has an owner. |
