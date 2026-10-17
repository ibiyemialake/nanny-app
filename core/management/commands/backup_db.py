"""
Back up the database to BACKUP_DIR and keep the newest BACKUP_KEEP copies.

    python manage.py backup_db                      # one backup now
    python manage.py backup_db --every-day-at 03:00 # keep running, one backup a day (used by Docker)

Uses SQLite's own backup, so it's safe while the app is running.
"""
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = "Back up the SQLite database and delete old backups."

    def add_arguments(self, parser):
        parser.add_argument("--to", default=settings.BACKUP_DIR, help="Folder for backups (default: BACKUP_DIR)")
        parser.add_argument("--keep", type=int, default=settings.BACKUP_KEEP, help="How many backups to keep")
        parser.add_argument("--source", default=str(settings.DATABASES["default"]["NAME"]), help=None)
        parser.add_argument("--every-day-at", metavar="HH:MM", help="Keep running and back up daily at this time")

    def handle(self, *args, **options):
        folder, keep, source = Path(options["to"]), options["keep"], Path(options["source"])
        if keep < 1:
            raise CommandError("--keep must be at least 1.")
        if not options["every_day_at"]:
            self.backup(source, folder, keep)
            return
        try:
            at = datetime.strptime(options["every_day_at"], "%H:%M").time()
        except ValueError:
            raise CommandError("--every-day-at must look like 03:00.")
        self.stdout.write(f"Backing up {source} to {folder} every day at {at:%H:%M}, keeping {keep}.")
        while True:
            now = timezone.localtime()
            next_run = timezone.make_aware(datetime.combine(now.date(), at))
            if next_run <= now:
                next_run = timezone.make_aware(datetime.combine(now.date() + timedelta(days=1), at))
            time.sleep((next_run - now).total_seconds())
            try:
                self.backup(source, folder, keep)
            except Exception as error:  # keep the loop alive; the next night may work
                self.stderr.write(f"Backup failed: {error}")

    def backup(self, source, folder, keep):
        if not source.exists():
            raise CommandError(f"No database at {source}.")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"db-{timezone.localtime():%Y-%m-%d-%H%M%S}.sqlite3"
        partial = target.with_suffix(".partial")
        with sqlite3.connect(source) as src, sqlite3.connect(partial) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise CommandError("The backup failed its integrity check.")
        src.close()
        dst.close()
        partial.rename(target)
        for old in sorted(folder.glob("db-*.sqlite3"))[:-keep]:
            old.unlink()
        self.stdout.write(f"Backed up to {target}")
