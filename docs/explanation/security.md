# Security

Duebell is a sandbox demo, but it can be hosted for several people. This page explains what protects them and what does not. The design is in [ADR 0008](../adr/0008-sign-in-and-ownership.md).

## Sign-in

- A middleware turns away every request without a valid session, except four public paths: sign-in, `/healthz`, static files and the PayPal webhook. It denies by default, so a new route is protected without extra code. A test lists every route and checks this.
- Duebell hashes passwords with scrypt and a random salt. Each hash records its cost, so the cost can rise later.
- The session is a cookie holding the user ID and an expiry, signed with `SECRET_KEY`. It is HttpOnly, SameSite=Lax, and Secure when `COOKIE_SECURE=true`. It lasts 12 hours.
- SameSite=Lax also stops other sites from posting forms with the cookie.
- Sign-in needs no cookie, so it checks the `Origin` header instead. A post whose `Origin` names a host other than the one it was sent to gets `403`, which stops a hostile page from signing a victim into the attacker's account. Duebell allows a post with no `Origin`, since some clients leave it out. The referrer policy is `same-origin`: it sends nothing to other sites, and it lets a browser name its own origin. Under `no-referrer` a browser sends `Origin: null` on every form post, which the check refuses.
- Five sign-in attempts within 15 minutes lock an email until the oldest is 15 minutes old. Each attempt is counted before its password is checked, so attempts sent at the same moment cannot slip past the limit. A correct password clears the count. An unknown email takes the same time and gets the same message as a wrong password.
- The lockout tracks at most 10,000 emails. It drops entries older than 15 minutes, and when full it drops the least recent email that is not locked, so a flood of new emails cannot free a locked one.
- There is no public sign-up. Whoever runs the server adds users with `scripts/create_user.py`.

## Ownership

Every client, invoice, reminder, schedule, event and risk comparison belongs to one user. Every function that lists records, or fetches one by ID, takes a scope with no default: one user's ID, or `EVERY_USER` for the scheduler. Passing `None` raises an error, so a forgotten scope stops the request instead of showing everyone's data. A request for another user's record gets the same `404` as a missing one.

Creating a record takes its owner explicitly. Only the offline evals create records with no owner. A few helpers, such as a client's history, filter by an ID the caller has already checked, so they take no scope of their own.

A webhook event belongs to the owner of the invoice it touches.

## Other limits

- In `sandbox` and `toolkit` modes, Duebell asks PayPal to check each webhook's signature before changing anything. Mock mode has no signature to check, so it refuses every webhook with `401` and stores nothing, unless `MOCK_WEBHOOKS` is on. Tests and the local demo turn it on; the Render blueprint leaves it off, so nobody can post fake events to a hosted mock demo. See [configuration](../reference/configuration.md#modes).
- Duebell stores each event once by its PayPal ID, so a replay does nothing, and it refuses webhook bodies over 1 MB.
- The model's output is untrusted. It may only fill six known fields, and the same validation as the manual form checks them ([ADR 0004](../adr/0004-llm-as-form-filler.md)).
- Amounts are capped, currencies come from a list, and text fields have length limits. See [configuration](../reference/configuration.md).
- Every response carries security headers and a content security policy. See below.
- Secrets live in `.env`, which git ignores.

## Content security policy

The policy tells the browser what the page may load and run:

```
default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'
```

- Scripts run only from Duebell's own `/static` folder. The browser refuses inline script, `on...=` handlers and scripts from any other host, so text an attacker slips into the page cannot run.
- Duebell serves AG Grid and htmx itself, from `app/static/vendor/`, instead of loading them from a CDN. The README there records each file's source, version and SHA-256 hash, and a test checks the files still match.
- The dashboard's own code lives in `app/static/dashboard.js`. The page hands it data in a `<script type="application/json">` block and in `data-` attributes, which the browser does not run.
- Styles still allow `'unsafe-inline'`. AG Grid and htmx add `<style>` tags and `style` attributes as they run, and would break without it. Injected CSS can change how the page looks but cannot run code.
- The page may fetch only from Duebell itself, and no other site may frame it.

`tests/test_csp.py` fails if the script rule loosens, if the policy names another host, or if a template gains inline script.

## Known gaps

- A session cannot be revoked before it expires, except by changing `SECRET_KEY`, which signs everyone out.
- The lockout lives in process memory. A restart clears it.
- There is no password reset and no email check on new users.
- SQLite and a single process: no running several copies of the app.

## Before hosting

1. Set a long random `SECRET_KEY`.
2. Serve over HTTPS and set `COOKIE_SECURE=true`.
3. Set your own `DEMO_PASSWORD`. `scripts/seed.py --require-password` refuses to seed without one.
4. Leave `MOCK_WEBHOOKS` off.

[Deploy to Render](../how-to/deploy-to-render.md) does all four.
