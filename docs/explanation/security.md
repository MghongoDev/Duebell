# Security

Duebell is a sandbox demo, but it can be hosted for several people. This page explains what protects them and what does not. The design is in [ADR 0008](../adr/0008-sign-in-and-ownership.md).

## Sign-in

- A middleware turns away every request without a valid session, except four public paths: sign-in, `/healthz`, static files and the PayPal webhook. It denies by default, so a new route is protected without extra code. A test lists every route and checks this.
- Duebell hashes passwords with scrypt and a random salt. Each hash records its cost, so the cost can rise later.
- The session is a cookie holding the user ID and an expiry, signed with `SECRET_KEY`. It is HttpOnly, SameSite=Lax, and Secure when `COOKIE_SECURE=true`. It lasts 12 hours.
- SameSite=Lax also stops other sites from posting forms with the cookie.
- Five failed sign-ins lock an email for 15 minutes. An unknown email takes the same time and gets the same message as a wrong password.
- There is no public sign-up. Whoever runs the server adds users with `scripts/create_user.py`.

## Ownership

Every client, invoice, reminder, schedule, event and risk comparison belongs to one user. Every function that lists records, or fetches one by ID, takes a scope with no default: one user's ID, or `EVERY_USER` for the scheduler. Passing `None` raises an error, so a forgotten scope stops the request instead of showing everyone's data. A request for another user's record gets the same `404` as a missing one.

Creating a record takes its owner explicitly. Only the offline evals create records with no owner. A few helpers, such as a client's history, filter by an ID the caller has already checked, so they take no scope of their own.

A webhook event belongs to the owner of the invoice it touches.

## Other limits

- Duebell asks PayPal to check each webhook's signature before changing anything. It stores each event once by its PayPal ID, so a replay does nothing, and it refuses webhook bodies over 1 MB.
- The model's output is untrusted. It may only fill six known fields, and the same validation as the manual form checks them ([ADR 0004](../adr/0004-llm-as-form-filler.md)).
- Amounts are capped, currencies come from a list, and text fields have length limits. See [configuration](../reference/configuration.md).
- Every response carries security headers and a content security policy.
- Secrets live in `.env`, which git ignores.

## Known gaps

- A session cannot be revoked before it expires, except by changing `SECRET_KEY`, which signs everyone out.
- The lockout lives in process memory. A restart clears it.
- There is no password reset and no email check on new users.
- SQLite and a single process: no running several copies of the app.

## Before hosting

1. Set a long random `SECRET_KEY`.
2. Serve over HTTPS and set `COOKIE_SECURE=true`.
3. Set your own `DEMO_PASSWORD`. `scripts/seed.py --require-password` refuses to seed without one.

[Deploy to Render](../how-to/deploy-to-render.md) does all three.
