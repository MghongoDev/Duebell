# Security

Duebell is a sandbox demo, but it can be hosted for several people. This page explains what protects them and what does not. The design is in [ADR 0008](../adr/0008-sign-in-and-ownership.md).

## Sign-in

- A middleware turns away every request without a valid session, except six public paths: sign-in, sign-out, sign-up, `/healthz`, static files and the PayPal webhook. Sign-out is public so an expired session can still sign out; it only clears the cookie. A page or form post without a session redirects to sign-in, and a JSON route answers `401`. It denies by default, so a new route is protected without extra code. A test lists every route and checks this.
- Duebell hashes passwords with scrypt and a random salt. Each hash records its cost, so the cost can rise later.
- The session is a cookie holding the user ID, the user's session key and an expiry, signed with `SECRET_KEY`. It is HttpOnly, SameSite=Lax, and Secure when `COOKIE_SECURE=true`. It lasts 12 hours.
- Each user has a random session key, stored with the user. The middleware accepts a cookie only while its key matches the user's current key. User IDs can repeat: a demo reset empties the tables, and SQLite then gives the next user the ID of a deleted one. The new user has a new key, so a cookie issued to the deleted user does not sign anyone in.
- Giving a user a new session key signs them out everywhere. Duebell has no button for this yet; a password change flow could do it.
- A database from an older build has no session keys. On startup Duebell adds the column and gives every user a key. Cookies issued before the upgrade hold no key, so every existing session ends once and each user signs in again.
- SameSite=Lax also stops other sites from posting forms with the cookie.
- Sign-in needs no cookie, so it checks the `Origin` header instead. A post whose `Origin` names a host other than the one it was sent to gets `403`, which stops a hostile page from signing a victim into the attacker's account. Duebell allows a post with no `Origin`, since some clients leave it out. The referrer policy is `same-origin`: it sends nothing to other sites, and it lets a browser name its own origin. Under `no-referrer` a browser sends `Origin: null` on every form post, which the check refuses.
- Five sign-in attempts within 15 minutes lock an email until the oldest is 15 minutes old. Each attempt is counted before its password is checked, so attempts sent at the same moment cannot slip past the limit. A correct password clears the count. An unknown email takes the same time and gets the same message as a wrong password.
- Sign-in refuses an email no user can have, one over 254 characters or without an `@`, and a password over 1,024 characters, before the lockout counts the attempt or hashes the password. So junk cannot fill the table, and locking an email costs a full password hash per attempt. Both get the same message as a wrong password. `scripts/create_user.py` applies the same limits.
- The lockout tracks at most 10,000 emails. It drops attempts older than 15 minutes, and an email with none left. When the table is full and a new email signs in, it drops the least recent email that is not locked. It never drops a locked email, so a flood of new emails cannot free one. If every email in the table is locked, sign-in refuses any new email with the same "Too many failed attempts" message and stores nothing; emails already in the table carry on as before.
- A full table of locked emails frees itself. Each locked email unlocks when its oldest attempt turns 15 minutes old, and the next new email takes its place. So once a flood stops, new emails can sign in again within 15 minutes. To keep the table full, an attacker must lock 10,000 emails every 15 minutes: 50,000 sign-in attempts, each a full password hash.
- Whoever runs the server adds users with `scripts/create_user.py`, unless sign-up is on. See below.

## Sign-up

Sign-up is off unless `SIGNUP_ENABLED` is on. While it is off, `/signup` answers `404` and the sign-in page does not link to it. The Render blueprint leaves it off.

- Sign-up refuses the same emails and passwords as `scripts/create_user.py`. A new user gets an empty account and a new session key.
- The form passes the same `Origin` check as sign-in.
- Sign-up does not sign the visitor in. Whether the email is new or already has an account, the visitor sees the same sign-in page with the same message, and gets no cookie. A taken email changes nothing about its account, and costs the same password hash as a new one, so the timing is close too.
- This hides less than it seems. Duebell does not check that a visitor owns the email. So anyone can sign up with an email and a password of their choosing, then try to sign in with it. If that works, the email was free, and they now hold an account under it. If it fails, the email was taken. Each such test costs one sign-up and one counted sign-in attempt. Hiding it fully needs an email that proves ownership before the account exists.
- The whole app accepts 20 sign-up attempts per 15 minutes. Each sign-up names a new email, so a count per email, as sign-in uses, would never fill. One count for everyone caps how fast anyone can add accounts or test emails. The cost is that someone who sends many attempts also turns honest visitors away, with `429`, until the oldest attempt is 15 minutes old. Sign-up refuses bad input before it counts, so typos do not use up the count. Like the lockout, the count lives in process memory.

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

- Signing a single user out everywhere means giving them a new session key in the database; there is no route or script for it. Changing `SECRET_KEY` signs everyone out.
- The lockout lives in process memory. A restart clears it.
- Anyone can keep the demo account locked by sending five wrong passwords every 15 minutes. The lockout counts attempts per email, not per client. Counting per client address would mean trusting the `X-Forwarded-For` header that Render's proxy adds, and any client can forge that header, so an attacker could make unlimited guesses by sending a new address each time. Duebell keeps counting per email.
- There is no password reset and no email check on new users. With sign-up on, anyone can make accounts under emails they do not own, and can learn whether an email is registered, as described above.
- SQLite and a single process: no running several copies of the app.

## Before hosting

1. Set a long random `SECRET_KEY`.
2. Serve over HTTPS and set `COOKIE_SECURE=true`.
3. Set your own `DEMO_PASSWORD`. `scripts/seed.py --require-password` refuses to seed without one.
4. Leave `MOCK_WEBHOOKS` off.

[Deploy to Render](../how-to/deploy-to-render.md) does all four.
