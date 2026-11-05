# Deploy to Render

`render.yaml` describes one free Render web service in mock mode. A public demo needs no PayPal, model or email credentials.

1. Push the repository to GitHub.
2. In Render, create a **Blueprint** from the repository.
3. When Render asks for `DEMO_PASSWORD`, enter one of your own. Do not reuse the default from the README.
4. Wait for the deploy, then open the service URL and sign in as `demo@duebell.test`.

The blueprint also:

- generates `SECRET_KEY`,
- sets `COOKIE_SECURE=true`, since Render serves HTTPS,
- starts with `scripts/seed.py --if-empty --require-password`, then the app.

## What the start command does

It seeds the demo only when the database has no users, so a restart keeps everyone's data. It refuses to seed without `DEMO_PASSWORD`, and the deploy fails with "Set DEMO_PASSWORD." in the log.

The free plan's disk is wiped on each deploy, so each deploy starts from the seed. For data that lasts, attach a Render disk and point `DATABASE_URL` at a file on it.

## Add users

Run `scripts/create_user.py` from a shell on the service. Render offers a shell on paid instance types only. On the free plan, the demo user is the only account. See [Add a user](add-a-user.md).

## Turn on real services

Set the variables for [the PayPal sandbox](connect-paypal-sandbox.md), [a live model](use-a-live-model.md) or [email](send-reminders-by-email.md) in the Render dashboard. Webhooks can use the service URL directly, with no tunnel.
