# Add a user

There is no sign-up page. Whoever runs the server adds each user from the command line.

```bash
.venv/bin/python scripts/create_user.py you@example.com
```

The script asks for a password of 10 to 1,024 characters. To pass it without a prompt, set `DUEBELL_PASSWORD` for that one command.

Each user sees only their own clients, invoices, reminders and schedules. Two users may bill the same client email.

## Adopt an older database

A database from before sign-in existed has records with no owner. Nobody can see them. Give them to one user:

```bash
.venv/bin/python scripts/create_user.py you@example.com --claim
```

If no user has that email, the script asks for a password, creates the user, then claims. If the user already exists, the script asks for no password and gives the records to that user. Without `--claim`, an email that already has a user stops the script with "A user with that email already exists."

The script prints what it claimed, for example `Claimed 7 client, 27 invoice, 2 reminder.` Run `--claim` once. Later records always have an owner.

## Change the demo password

The seed creates `demo@duebell.test` with `DEMO_PASSWORD`, or `duebell-demo-2026` if that is unset. To change it, set `DEMO_PASSWORD` and run `scripts/reset_demo.py`. That also resets the demo data.

There is no password change for other users yet.
