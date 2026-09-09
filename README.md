# Disciple.live

A working first release of the original discipleship connection app: find a discipler, request a connection, schedule a one-hour meeting, open video, and study shared resources. This continues the Flask app in PR #1. The separate marketing concept in PR #2 has not been merged; the homepage here describes the actual available workflows.

## Included

- Member registration and sign-in; separate discipler, learner, and administrator views.
- Discipler profiles, personal introductions, and accept/decline requests.
- Time-zone-aware availability; future times only, hour-long spacing, booking restricted to accepted connections, serialized bookings to prevent double-booking, cancellation, and removal of open times.
- Meeting pages limited to the participants and administrators, with unpredictable Jitsi room links and the chosen study resource.
- Searchable member resource library; administrator document upload, HTTPS links, removal, and authenticated downloads.
- Profile editing, time zone selection, password changes, and revocation of older sessions on password change/reset.
- CSRF protection, bounded uploads, secure session cookie defaults, security headers, sign-in throttling, and no default administrator password.
- Docker/Gunicorn packaging with a persistent database/upload volume.

## Local use

Python 3.12 is the tested runtime. Create a virtual environment and install the pinned dependencies:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export COOKIE_SECURE=0
flask --app app init-db
flask --app app create-admin
flask --app app run
```

Only use `COOKIE_SECURE=0` for local HTTP development. Keep the same secret across production restarts; rotate it to invalidate all login sessions. The app refuses to start without a secret of at least 32 characters. Production secrets belong in the host's secret manager or an untracked `.env` file, never in Git.

Members create accounts at `/register`. Set the correct time zone on `/profile` before entering availability; newly created accounts default to UTC. The existing `disciplier` spelling is retained only in database role values for compatibility.

## Production hosting

The existing Python app needs a Python/Docker host with durable disk storage. It is not a Cloudflare Worker bundle. No live deployment or domain change is included in this branch.

1. Copy `.env.example` to `.env` and set a generated `SECRET_KEY`.
2. Run `docker compose up --build -d` on the host. The container initializes the database and runs Gunicorn with debug disabled.
3. Run `docker compose exec app flask --app app create-admin` and choose the administrator credentials interactively.
4. Configure an HTTPS reverse proxy for `disciple.live` to `127.0.0.1:8000`. The Compose port is intentionally bound to loopback. Configure the DNS record for the chosen host and provision its TLS certificate.
5. Keep the `disciple_data` volume. The app stores its database and uploaded resources in `/data`; an ephemeral filesystem will lose member records and files. Use one host/volume for this SQLite release, not multiple independent replicas.
6. Configure regular backups of both the database and uploads and verify restoration. Stop writes or use SQLite's backup API to obtain a consistent database copy.
7. Use `/health` for a database connectivity check. Create two ordinary test accounts and verify a real video call from two devices before inviting members.

Docker packaging is supplied but has not been built or deployed in this workspace. No live camera/microphone test or browser visual QA has been performed.

## Video behavior

Video currently opens a separate Jitsi window so the shared resource can remain visible. This avoids depending on third-party sign-in inside an iframe. Jitsi requires the person creating a room to authenticate separately; a disciple.live login does not authenticate to Jitsi. See [Jitsi's authentication explanation](https://jitsi.org/blog/authentication-on-meet-jit-si/).

Access control protects the app's meeting page, not the external provider. Anyone who receives a Jitsi link may attempt to join it. Participants should keep links private and use the provider's room protections. Provider-bound participant authentication and guaranteed private video would require a configured video service and credentials; these are not simulated here.

## Account recovery and launch limits

Password recovery is currently administrator-assisted:

```bash
flask --app app reset-password
```

Verify the member's identity before using it. Passwords are prompted without echo. Changing/resetting a password invalidates older signed-in sessions. Email verification, emailed password-reset links, reminders, payments, background checks/mentor approval, and group meetings are not implemented. Registration as a discipler does not imply vetting. This release supports the requested one-to-one workflow; it should not be represented as providing those additional services.

## Existing database migration

Back up the original database and uploaded files first. If an earlier version has `disciple_live.db` and `uploads/` beside `app.py`, copy those into the configured `DATA_DIR` (default `instance/` locally, `/data` in Docker) before initialization. The app stops if a legacy database is found but no database exists at the new location; it will not silently start a fresh account database.

`init-db` preserves existing records and adds time-zone/authentication-version columns, indexes, and the sign-in attempt table. Older naive meeting timestamps are interpreted as UTC; verify or recreate historical local-time availability before launch. A legacy `admin@disciple.live` account still using the published `admin123` password is disabled by assigning a random password; use `reset-password` to set a new one after verifying ownership. No new demo account is created.

## Verification

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The tests use a temporary database and exercise registration, connection acceptance, resource publication/search, booking, meeting access, cancellation/rebooking, authenticated downloads, resource deletion, invalid inputs, CSRF rejection, account permissions, password/session revocation, login throttling, concurrent booking, time zones, and daylight-saving invalid/ambiguous times. They do not call Jitsi or test live browser media.

[Flask's Gunicorn deployment documentation](https://flask.palletsprojects.com/en/stable/deploying/gunicorn/) describes the production process used by the container.
