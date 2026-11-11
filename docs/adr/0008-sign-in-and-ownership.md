# 0008 Sign in with a signed cookie, and give every record an owner

## Status

Accepted

## Date

2026-11-05

## Context

Until issue #24, Duebell had no login and one implicit user, so it could not be hosted safely. Hosting it for more than one person needs two things: a way to know who is asking, and a guarantee that one person never sees or changes another's clients, invoices or reminders. The app is one process over SQLite (ADR 0005), and every dependency added so far has had to earn its place (ADR 0001).

## Decision

- Hash passwords with the standard library's scrypt. Each hash records its cost, so the cost can rise later.
- Keep the session in an HMAC-signed cookie holding the user id and an expiry. It is HttpOnly and SameSite=Lax, which also blocks cross-site form posts. There is no server-side session table.
- Enforce sign-in in one middleware that denies by default. Only sign-in, the health check, static files and the PayPal webhook are public. The webhook keeps its signature check.
- Give every table a `user_id`. Query helpers filter by it, and a lookup by id returns nothing for another user's row, so the route answers 404 exactly as for a missing record.
- Make client email unique per user, not globally, so two users can bill the same client.
- Add users from the command line. `--claim` hands ownerless rows from an older database to a user.

## Alternatives considered

- **An auth library or an external identity provider.** Rejected for now because the app needs one sign-in form, and either choice adds a dependency and setup the demo does not need.
- **Server-side sessions.** Rejected because they add a table and cleanup for no gain at this size. The cost is that a session cannot be revoked before it expires, except by rotating `SECRET_KEY`.
- **Public sign-up.** Rejected because a hosted demo would then accept anyone, and there is no email verification or password reset yet.
- **Check ownership in each route by hand.** Rejected as the only defence because a new route could forget. Default-deny covers sign-in, and the shared lookup helper covers ownership.

## Consequences

- A test enumerates every route and checks that an anonymous request is turned away, so a new route cannot slip through unnoticed.
- The background scheduler still works across all users. Each refit trains on one user's invoices only.
- The sign-in lockout is per process and resets on restart.
- An existing database keeps working: new columns and indexes are added at startup, and `--claim` assigns its rows.

## Revision, 2026-11-05

A review found that the query helpers took `user_id=None` by default, and `None` meant every user. A route that forgot the argument would have read everyone's records, which is the risk this record set out to remove. Every function that lists records or fetches one by ID now takes a scope with no default: a user ID, or `EVERY_USER` (`app/ownership.py`), which only the scheduler uses. Passing `None` raises `TypeError`. A refit refuses `EVERY_USER`, so it always trains on one user's invoices.

The same review moved the Render start command from a full reset to `scripts/seed.py --if-empty --require-password`, so a restart keeps data and a hosted demo cannot use the published password.

## Revision, 2026-11-10

The note above says the `--if-empty` start command lets a restart keep data. That holds only on a persistent disk. The blueprint runs on Render's free plan, which has no disk: the SQLite file is lost at each deploy, restart and spin-down after 15 idle minutes, and the next start seeds a fresh demo. A persistent disk needs a paid instance type. `--if-empty` still keeps data on such a disk, and `--require-password` still applies everywhere (issue #87).

## Revision, 2026-11-11

The decision above names four public paths. Since issue #84 there is a fifth: `POST /logout`. It only clears the session cookie and redirects to sign-in, so it reads and changes no records. It is public so that a person whose session has expired can still sign out.
