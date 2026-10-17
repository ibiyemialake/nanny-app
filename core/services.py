"""Business logic: pay periods, schedules, forgotten check-outs, tasks, logging."""
import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.mail import send_mail
from django.db.models import Q
from django.utils import timezone

from .models import CENT, AuditLog, PayRecord, StaffMember, Task, TaskCompletion, TimeEditRequest, TimeEntry


# --- Logging & notifications ---------------------------------------------------

def log(household, actor, action, details=""):
    AuditLog.objects.create(household=household, actor=actor, action=action, details=details)


def notify_owner(household, subject, body):
    to = household.alert_email
    if to:
        send_mail(subject, body, None, [to], fail_silently=True)


def local_dt(day, t):
    return timezone.make_aware(datetime.combine(day, t))


def fmt_time(dt):
    return timezone.localtime(dt).strftime("%-I:%M %p")


def fmt_dt(dt):
    return timezone.localtime(dt).strftime("%a %b %-d, %-I:%M %p") if dt else "—"


# --- Pay periods -----------------------------------------------------------------

MID_MONTH_PAYDAY = 15


def period_containing(staff, day):
    """Return (start, end) of the pay period containing `day`. Payday is `end`.

    Paydays are the 15th and the last day of every month, so periods run 1st–15th and 16th–end of month.
    """
    if day.day <= MID_MONTH_PAYDAY:
        return day.replace(day=1), day.replace(day=MID_MONTH_PAYDAY)
    last = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=MID_MONTH_PAYDAY + 1), day.replace(day=last)


def recent_periods(staff, count, today=None):
    """The current period followed by `count - 1` earlier ones."""
    today = today or timezone.localdate()
    periods = []
    day = today
    for _ in range(count):
        start, end = period_containing(staff, day)
        periods.append((start, end))
        day = start - timedelta(days=1)
    return periods


@dataclass
class PeriodSummary:
    staff: StaffMember
    start: date
    end: date
    entries: list = field(default_factory=list)
    seconds: int = 0
    amount: Decimal = Decimal("0.00")
    open_entries: int = 0
    pay_record: PayRecord = None
    late_checkouts: int = 0
    unscheduled: int = 0

    @property
    def to_review(self):
        return self.late_checkouts + self.unscheduled

    @property
    def pay_date(self):
        return self.end

    @property
    def hours(self):
        return (Decimal(self.seconds) / 3600).quantize(CENT)

    @property
    def is_payday(self):
        return self.end == timezone.localdate()

    @property
    def is_due(self):
        return self.end <= timezone.localdate()

    @property
    def days_until_payday(self):
        return (self.end - timezone.localdate()).days


def summarize_period(staff, start, end):
    entries = list(
        staff.entries.filter(check_in__gte=local_dt(start, time.min),
                             check_in__lt=local_dt(end + timedelta(days=1), time.min))
        .order_by("check_in")
    )
    summary = PeriodSummary(staff=staff, start=start, end=end, entries=entries)
    raw_amount = Decimal("0")
    for entry in entries:
        if entry.late_checkout_pending:
            summary.late_checkouts += 1
        if entry.unscheduled_pending:
            summary.unscheduled += 1
        if not entry.is_payable:
            continue
        if entry.check_out:
            summary.seconds += entry.duration_seconds
            raw_amount += Decimal(entry.duration_seconds) * entry.hourly_rate / 3600
        else:
            summary.open_entries += 1
    summary.amount = raw_amount.quantize(CENT)
    summary.pay_record = staff.pay_records.filter(pay_date=end).first()
    return summary


def current_summary(staff):
    return summarize_period(staff, *period_containing(staff, timezone.localdate()))


def payday_summaries(staff, today=None):
    """Periods to bring up on the dashboard: today's payday (paid or not), and earlier paydays not yet paid."""
    today = today or timezone.localdate()
    summaries = []
    for start, end in recent_periods(staff, 3, today):
        if end > today:
            continue
        summary = summarize_period(staff, start, end)
        if end == today or (not summary.pay_record and (summary.seconds or summary.open_entries)):
            summaries.append(summary)
    return summaries


# --- Schedules & time entries ------------------------------------------------------

def slot_for(staff, now_local):
    """The scheduled slot for today whose start is closest to now."""
    slots = list(staff.schedule.filter(weekday=now_local.weekday()))
    if not slots:
        return None
    minutes_now = now_local.hour * 60 + now_local.minute
    return min(slots, key=lambda s: abs(s.start_time.hour * 60 + s.start_time.minute - minutes_now))


def blocking_open_entry(staff):
    """An open shift that stops a new check-in. Shifts with a correction waiting for review don't block."""
    return (staff.entries.filter(check_out__isnull=True)
            .exclude(edit_requests__status=TimeEditRequest.PENDING)
            .order_by("-check_in").first())


def checkout_deadline(entry, household):
    end = entry.scheduled_end_at
    if end:
        if end < entry.check_in:
            end = entry.check_in
        return end + timedelta(hours=household.forgotten_checkout_hours)
    return entry.check_in + timedelta(hours=12)


def flag_forgotten_checkouts(household):
    now = timezone.now()
    open_entries = TimeEntry.objects.filter(
        staff__household=household, check_out__isnull=True, flagged_forgotten=False).select_related("staff__user")
    for entry in open_entries:
        if now > checkout_deadline(entry, household):
            entry.flagged_forgotten = True
            entry.save(update_fields=["flagged_forgotten"])
            log(household, None, "Missed check-out flagged",
                f"{entry.staff.name} checked in {fmt_dt(entry.check_in)} and never checked out.")


def is_late_checkout(entry, household):
    """True if the check-out is more than the grace period after the scheduled end."""
    end = entry.scheduled_end_at
    return bool(end and entry.check_out
                and entry.check_out > max(end, entry.check_in) + timedelta(minutes=household.late_grace_minutes))


def resolve_late_checkout(entry, actor, decision, finished_at=None):
    """Accept the late check-out, set it back to the scheduled end, or set the time they actually finished."""
    household = entry.staff.household
    before = fmt_dt(entry.check_out)
    if decision == "accept":
        entry.late_checkout_status = TimeEntry.LATE_ACCEPTED
        log(household, actor, "Late check-out accepted", f"{entry.staff.name}: kept check-out at {before}.")
    else:
        if decision == "decline":
            entry.check_out = max(entry.scheduled_end_at, entry.check_in)
            entry.late_checkout_status = TimeEntry.LATE_DECLINED
            action = "Late check-out declined"
        else:
            entry.check_out = finished_at
            entry.late_checkout_status = TimeEntry.LATE_ADJUSTED
            action = "Late check-out changed"
        entry.edited = True
        log(household, actor, action, f"{entry.staff.name}: check-out {before} → {fmt_dt(entry.check_out)}.")
    entry.save(update_fields=["check_out", "late_checkout_status", "edited"])


def resolve_unscheduled(entry, actor, approve):
    """Approve or decline a shift worked on a day with no scheduled shift. Declined shifts aren't paid."""
    entry.unscheduled_status = TimeEntry.UNSCHEDULED_APPROVED if approve else TimeEntry.UNSCHEDULED_DECLINED
    entry.save(update_fields=["unscheduled_status"])
    log(entry.staff.household, actor, "Unscheduled shift approved" if approve else "Unscheduled shift declined",
        f"{entry.staff.name}: in {fmt_dt(entry.check_in)}, out {fmt_dt(entry.check_out)}"
        + ("." if approve else ". Not paid."))


# --- Tasks -----------------------------------------------------------------------

@dataclass
class TaskItem:
    task: Task
    date: date
    completion: TaskCompletion = None
    overdue: bool = False


def task_items(household, day, staff=None):
    """Tasks for `day`, plus earlier one-off tasks that were never ticked off."""
    tasks = Task.objects.filter(household=household, is_active=True).select_related("assigned_to__user")
    if staff:
        tasks = tasks.filter(Q(assigned_to=staff) | Q(assigned_to__isnull=True))
    tasks = list(tasks)
    completions = {
        (c.task_id, c.date): c
        for c in TaskCompletion.objects.filter(task__in=tasks).select_related("completed_by__user")
    }
    items = []
    for task in tasks:
        if task.occurs_on(day):
            items.append(TaskItem(task, day, completions.get((task.id, day))))
        elif (task.recurrence == Task.ONCE and task.due_date and task.due_date < day
              and (task.id, task.due_date) not in completions):
            items.append(TaskItem(task, task.due_date, None, overdue=True))
    items.sort(key=lambda i: (i.completion is not None, not i.overdue,
                              i.task.due_time or time.max, i.task.title.lower()))
    return items
