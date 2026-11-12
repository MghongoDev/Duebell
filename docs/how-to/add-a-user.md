# Add a user

Whoever runs the server can add each user from the command line. If `SIGNUP_ENABLED` is on, visitors can also sign up themselves; see [Let visitors sign up](#let-visitors-sign-up).

```bash
.venv/bin/python scripts/create_user.py you@example.com
```

The script asks for a password of 10 to 1,024 characters. To pass it without a prompt, set `DUEBELL_PASSWORD` for that one command.

Each user sees only their own clients, invoices, reminders and schedules. Two users may bill the same client email.

## Let visitors sign up

Sign-up is off by default, and `render.yaml` leaves it off. To turn it on, set `SIGNUP_ENABLED=true` and restart the app. The sign-in page then links to `/signup`. While it is off, `/signup` answers `404`.

A visitor enters an email and a password. Sign-up refuses the same emails and passwords as the script, with the same messages. A new account starts empty.

Sign-up does not sign the visitor in. It shows the sign-in page with the email filled in, and the visitor signs in with the password they chose. It shows that same page when the email already has an account, and changes nothing about that account, so the page does not say which emails are registered. See [security](../explanation/security.md#sign-up) for what this does and does not hide.

The whole app accepts 20 sign-up attempts per 15 minutes. After that, sign-up answers `429` until the oldest attempt is 15 minutes old.

## Adopt an older database

A database from before sign-in existed has records with no owner. Nobody can see them. Give them to one user:

```bash
.venv/bin/python scripts/create_user.py you@example.com --claim
```

If no user has that email, the script asks for a password, creates the user, then claims. If the user already exists, the script asks for no password and gives the records to that user. Without `--claim`, an email that already has a user stops the script with "A user with that email already exists."

The script prints what it claimed, for example `Claimed 7 client, 27 invoice, 2 reminder.` Run `--claim` once. Later records always have an owner.

## Change the demo password

The seed creates `demo@duebell.test` with `DEMO_PASSWORD`, or `duebell-demo-2026` if that is unset. To change it, set `DEMO_PASSWORD` and run `scripts/reset_demo.py`. That also resets the demo data, and removes every other user and their records.

You can run it while the app runs. The script empties each table in the same database file, so the app keeps working and the new password works at once. Two things live in the app's memory and do not reset:

- A browser that was signed in stays signed in until its session ends, after 12 hours.
- If five sign-ins failed in the last 15 minutes, the email stays locked out. Wait, or restart the app.

## Reset a forgotten password

A user who forgot their password clicks **Forgot password?** on the sign-in page and enters their email. Duebell emails a link that works once, within 30 minutes. The link opens a page to choose a new password of 10 to 1,024 characters. The new password signs the user out of every other browser.

The page shows the same message whether or not the email has an account. An email can ask for five links within 15 minutes; further requests send nothing.

Reset links need email:

- In `smtp` mode, set `PUBLIC_URL` to the address users open Duebell at, so the link points there. Without it, Duebell sends no link and logs a warning. See [send reminders by email](send-reminders-by-email.md) for the SMTP settings.
- In `mock` mode no email goes out. The link appears in the server log, as a line that starts `Mock email: password reset link for`. Copy it from there.
