# Nanny Check-in

A web app my family uses every day with our two nannies. It runs on our home server.

## Why I built it

I have two nannies, and managing them was getting hard. Hours, tasks, what the baby ate and who was owed what lived in texts and my head. I wanted one place for it that we could all use from our phones.

## What it does

The nannies get one big button to check in and out. Before checking out they fill in a quick handover: feeds (in ml), naps, diaper changes, the time of the last of each, the baby's mood and any notes. They also tick off tasks and see their hours and pay, but only on our home Wi-Fi.

I get a dashboard with who's here, the latest handovers and anything waiting on me, and I set schedules, rates and tasks.

The part I'm proudest of: I only pay for scheduled time unless I agree to more. Nobody can check in more than 10 minutes early, and an early check-in counts from the scheduled start. A late check-out or an unscheduled day waits for me to accept it, decline it or set the real finish time, and that pay period can't be marked paid until I do.

Payday is the 15th and the last day of the month. When I tap Paid, the nanny sees it straight away. Shifts are never deleted, and every change is logged.

## Tech stack

- Django 5.2 with server-rendered templates and barely any JavaScript. It's mostly forms on phones, so a frontend framework wasn't worth it.
- SQLite, because it's one household. Backups use SQLite's own backup API, so they're safe while the app runs.
- gunicorn behind Caddy in Docker Compose, with nightly backups. Optional HTTPS uses a Route 53 DNS challenge, so nothing is opened to the internet.
- 45 tests, mostly on pay periods and the review rules, since that's where bugs cost money.

Caleb, my husband, a platform engineer with ten years in DevOps and cloud architecture, did the Docker, Caddy, backup and HTTPS setup and got it running on our home server.

## What's next

Baby analytics: feeds, naps and diapers over time, built from the handovers we already collect.

## Running it locally

You need Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open http://localhost:8000 and the first visit walks you through setting up a home and an owner account.

Without email settings, invite emails are printed in the terminal. You can also copy an invite link from the Nannies page and open it in a private window to try the nanny side.

Run the tests with `python manage.py test core`.

## The home Wi-Fi restriction

A website can't see which Wi-Fi network a phone is on, so the app checks the device's network address instead.

1. The main protection is where it runs: on our home server, not exposed to the internet. Away from home, the address just doesn't exist.
2. On top of that, `STAFF_ALLOWED_NETWORKS` (for example `192.168.1.0/24`) limits nannies to the home network. Signing in from anywhere else is refused, and a nanny who leaves the network is signed out.
3. The owner account is never restricted. I check in on things from work over Tailscale. Only add Tailscale's range (`100.64.0.0/10`) to `STAFF_ALLOWED_NETWORKS` if nannies should use it too. Ours don't.

The Settings page shows whether the restriction is on and what address your device has.

## Deploying

It ships with Docker Compose: the app (gunicorn), Caddy in front of it, and a nightly database backup.

1. Copy this folder to the server.
2. Copy `.env.example` to `.env` and fill it in. Leave out `DATA_DIR` and `BACKUP_DIR`; Docker sets those.
3. Create the backups folder yourself so you own it: `mkdir -p backups`.
4. Run `docker compose up -d --build`. Migrations run on start.
5. Open `http://<server address>:8088` straight away and set up your home. Until you do, anyone on the network could.
6. Give the server a fixed address on your router, and don't set up port forwarding.

To update, copy the new code over and run `docker compose up -d --build` again.

**Backups.** Every night at `BACKUP_TIME` a checked copy of the database goes to `./backups`, and the newest `BACKUP_KEEP` are kept. `docker compose run --rm backup python manage.py backup_db` makes one right now. Copy that folder off the server now and then. To restore, stop the app, copy a backup over `db.sqlite3` in the `data` volume, delete the `db.sqlite3-wal` and `db.sqlite3-shm` files next to it, and start it again.

**HTTPS.** Out of the box it's plain HTTP on the home network. If your domain is on Route 53, you can get a real certificate without opening anything to the internet:

1. In Route 53, add an A record such as `nanny.example.com` pointing at the server's home address (for example `10.0.0.92`).
2. Create an IAM user with the policy in `deploy/route53-policy.json` (replace `HOSTED_ZONE_ID` with your zone's ID) and make an access key for it.
3. In `.env`, set `COMPOSE_FILE=compose.yaml:compose.https.yaml`, `SITE_DOMAIN`, the two `AWS_` keys and `USE_HTTPS=1`. Add the domain to `ALLOWED_HOSTS`, add `https://<domain>` to `CSRF_TRUSTED_ORIGINS`, and change `SITE_URL` to `https://<domain>`.
4. Run `docker compose up -d --build`. Caddy proves you own the domain with a temporary DNS record and renews the certificate itself. The old `:8088` address redirects to the new one.

Some home routers block DNS answers that point at a home address ("DNS rebinding protection"). If the name doesn't resolve on your Wi-Fi, allow the domain in the router's settings.

Without Docker: install the requirements, set the variables from `.env`, run `python manage.py migrate` and `python manage.py collectstatic --noinput`, then `gunicorn homeapp.wsgi -b 0.0.0.0:8000`.

## Project layout

```
homeapp/settings.py      All configuration, read from environment variables
core/models.py           Home, nannies, schedules, shifts, handovers, tasks, payments, log
core/services.py         Pay periods, late and missed check-outs, review rules, task lists
core/middleware.py       Home network restriction
core/views_owner.py      Owner pages
core/views_staff.py      Nanny pages
core/management/         backup_db command
core/tests.py            Tests
compose*.yaml, deploy/   Docker, Caddy and HTTPS setup
```

The Django admin at `/admin/` works with the owner account as a fallback for anything the screens don't cover.
