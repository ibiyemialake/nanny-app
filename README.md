# Nanny Check-in

A small web app for running your home with domestic staff: check-ins and check-outs, schedules, tasks, notes, pay periods and paydays. Built with Django and SQLite, so it runs comfortably on a home server.

## What it does

**You (the owner)**

- Set up your home once, then invite nannies by email. They create their own password from the invite link.
- Set each nanny's weekly schedule and hourly rate. Everyone is paid on the 15th and on the last day of each month.
- See each nanny's Interac e-Transfer email or phone number, so you know where to send their pay.
- See who's here today, who arrived late or early, and the notes left at check-out.
- Create tasks: one-off, every day, or repeating on chosen days, for a specific nanny or whoever is on shift. Save frequent tasks as one-tap buttons, and use them for one-off or repeating tasks too.
- Review hours, fix shifts, add a missed shift by hand, and approve or decline time fixes nannies ask for.
- When a nanny checks out later than their scheduled end, accept the extra time, decline it (the shift ends at the scheduled time), or set the time they actually finished. That pay period can't be marked paid until you've reviewed it.
- Mark a pay period as paid. The amount and hours are saved as a record even if rates change later.
- Remove a nanny's access instantly. Their hours, notes and pay history stay.
- See every check-in, change and payment in the activity log.

**Nannies**

- One big button to check in and check out, with a live timer while they're on shift.
- Today's tasks, ticked off with an optional comment.
- A note for you at check-out.
- Hours and earnings for the current pay period, a payday message on payday, and past periods.
- Their Interac e-Transfer email or phone number for pay. They're asked for it when they join and can change it on their Hours & pay page.
- If they forget to check out, the app flags it and they can send the correct time for your approval.

**Records:** shifts are never deleted. Edits are marked as adjusted and logged with before and after values.

## Try it on your computer

You need Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open http://localhost:8000. The first visit asks you to set up your home and your account.

Without email settings, invite emails are printed in the terminal window. You can also copy the invite link from the Nannies page. To test as a nanny, open the link in a private browser window.

Run the tests with `python manage.py test core`.

## How the home Wi-Fi restriction works

Websites can't see the name of the Wi-Fi network a phone is on, so the app checks the device's network address instead.

1. **Main protection (deployment):** run the app only on your home server and don't expose it to the internet. Away from home, the address simply doesn't exist for the nannies.
2. **Second layer (built in):** set `STAFF_ALLOWED_NETWORKS` to your home network, for example `192.168.1.0/24`. A nanny who tries to sign in from anywhere else is refused, and a signed-in nanny who leaves the network is signed out.
3. **You are never restricted.** To check in on things from work, install Tailscale on the server and your phone (free for personal use). Then add Tailscale's range, `100.64.0.0/10`, to `STAFF_ALLOWED_NETWORKS` only if nannies should also use it. Normally they shouldn't, so leave it out.

The Settings page shows whether the restriction is on and what address your current device has.

## Deploying to your home server

Deployment is on hold until the server details are known. The short version:

1. Copy this folder to the server and install the requirements.
2. Copy `.env.example` to `.env` and fill it in. Load it into the environment however your setup prefers (systemd `EnvironmentFile=`, Docker `--env-file`, and so on).
3. Run `python manage.py migrate` and `python manage.py collectstatic --noinput`.
4. Serve with a production server such as `gunicorn homeapp.wsgi -b 0.0.0.0:8000` (add `gunicorn` to the requirements).
5. Give the server a fixed address on your router, and don't set up port forwarding.
6. Back up the `DATA_DIR` folder (it holds the whole database) every night.

## Settings you might change in the app

Settings page: how many minutes late counts as late, how long after a shift ends a missing check-out is flagged, and whether to email you about late check-ins.

## Project layout

```
homeapp/settings.py      All configuration, read from environment variables
core/models.py           Data: home, nannies, schedules, shifts, tasks, payments, log
core/services.py         Pay period maths, late/missed check-out logic, task lists
core/middleware.py       Home network restriction
core/views_owner.py      Your pages
core/views_staff.py      Nanny pages
core/templates/core/     Page templates
core/static/core/        Stylesheet
core/tests.py            Tests
```

The Django admin at `/admin/` is available to the owner account as a fallback for anything the screens don't cover.
