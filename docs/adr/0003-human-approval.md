# 0003 Require human approval before a reminder is sent

## Status
Accepted

## Date
2026-11-03

## Context
A badly worded payment reminder can damage a client relationship. An AI draft can be wrong about the amount, the due date, or the tone. The scheduler can run unattended. Sending without review would make the agent autonomous in a place where the cost of an error is social, not technical.

## Decision
Separate drafting from sending. `draft_reminders` finds eligible invoices and creates `PENDING` reminders. `send_reminder` is the only function that calls `gateway.send_reminder`, and it refuses anything that is not `PENDING`. The dashboard shows the full draft text with Approve and Dismiss. The background scheduler only drafts.

## Alternatives considered
- **Auto-send reminders for high-risk invoices.** Rejected because risk predicts lateness, not whether a message is appropriate today.
- **Send after a delay unless dismissed.** Rejected because silence is not approval, and the demo should not depend on the operator watching a timer.

## Consequences
- No reminder leaves the app without a click.
- The scheduler can run every 30 minutes without surprising anyone.
- A pending draft blocks a duplicate draft for the same invoice.
- Dismissed drafts can be drafted again on a later scan.
