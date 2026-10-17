import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

WEEKDAYS = [
    (0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"),
    (4, "Friday"), (5, "Saturday"), (6, "Sunday"),
]
CENT = Decimal("0.01")


class Household(models.Model):
    name = models.CharField(max_length=120)
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_household")
    late_grace_minutes = models.PositiveIntegerField(
        default=10, help_text="Check-ins more than this many minutes after the scheduled start count as late, and "
                              "check-outs more than this many minutes after the scheduled end need your review.")
    forgotten_checkout_hours = models.PositiveIntegerField(
        default=2, help_text="Flag a shift if no one has checked out this many hours after the scheduled end.")
    notify_on_late = models.BooleanField(default=False, help_text="Email me when someone checks in late.")
    notify_email = models.EmailField(blank=True, help_text="Where alerts go. Leave blank to use your login email.")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    @property
    def alert_email(self):
        return self.notify_email or self.owner.email


class StaffMember(models.Model):
    """Everyone is paid on the 15th and on the last day of each month."""
    household = models.ForeignKey(Household, on_delete=models.PROTECT, related_name="staff")
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="staff_profile")
    hourly_rate = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("20.00"))
    interac = models.CharField(max_length=254, blank=True,
                               help_text="Email or mobile number for Interac e-Transfer payments.")
    deactivated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["user__first_name", "user__email"]

    def __str__(self):
        return self.name

    @property
    def name(self):
        return self.user.get_full_name() or self.user.email

    @property
    def first_name(self):
        return self.user.first_name or self.name

    @property
    def is_active(self):
        return self.user.is_active


class ScheduleSlot(models.Model):
    staff = models.ForeignKey(StaffMember, on_delete=models.CASCADE, related_name="schedule")
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ["weekday", "start_time"]

    @property
    def overnight(self):
        return self.end_time <= self.start_time


class TimeEntry(models.Model):
    LATE_PENDING, LATE_ACCEPTED, LATE_DECLINED, LATE_ADJUSTED = "pending", "accepted", "declined", "adjusted"
    LATE_CHECKOUT_STATUSES = [
        (LATE_PENDING, "Waiting for review"), (LATE_ACCEPTED, "Accepted"),
        (LATE_DECLINED, "Set to scheduled end"), (LATE_ADJUSTED, "Finish time changed"),
    ]

    staff = models.ForeignKey(StaffMember, on_delete=models.PROTECT, related_name="entries")
    check_in = models.DateTimeField()
    check_out = models.DateTimeField(null=True, blank=True)
    hourly_rate = models.DecimalField(max_digits=7, decimal_places=2, help_text="Rate at the time of the shift.")
    checkout_note = models.TextField(blank=True)
    scheduled_start = models.TimeField(null=True, blank=True)
    scheduled_end = models.TimeField(null=True, blank=True)
    late_minutes = models.IntegerField(null=True, blank=True, help_text="Positive = late, negative = early.")
    flagged_forgotten = models.BooleanField(default=False)
    late_checkout_status = models.CharField(max_length=10, choices=LATE_CHECKOUT_STATUSES, blank=True,
                                            help_text="Set when someone checks out later than their scheduled end.")
    original_check_out = models.DateTimeField(null=True, blank=True,
                                              help_text="The check-out time they tapped, kept after a late check-out "
                                                        "is reviewed.")
    edited = models.BooleanField(default=False)
    created_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-check_in"]
        verbose_name_plural = "time entries"

    @property
    def is_open(self):
        return self.check_out is None

    @property
    def duration_seconds(self):
        if not self.check_out:
            return 0
        return max(0, int((self.check_out - self.check_in).total_seconds()))

    @property
    def amount(self):
        return (Decimal(self.duration_seconds) * self.hourly_rate / 3600).quantize(CENT)

    @property
    def scheduled_end_at(self):
        """The scheduled end as a date and time, on the day of check-in (or the next day for overnight shifts)."""
        if not self.scheduled_end:
            return None
        local_in = timezone.localtime(self.check_in)
        end = timezone.make_aware(datetime.combine(local_in.date(), self.scheduled_end))
        if self.scheduled_start and self.scheduled_end <= self.scheduled_start:
            end += timedelta(days=1)  # overnight shift
        return end

    @property
    def late_checkout_pending(self):
        return self.late_checkout_status == self.LATE_PENDING

    @property
    def late_checkout_minutes(self):
        """How long after the scheduled end they checked out, by the time they tapped."""
        tapped, end = self.original_check_out or self.check_out, self.scheduled_end_at
        if not tapped or not end:
            return None
        return round((tapped - end).total_seconds() / 60)

    @property
    def pending_request(self):
        return self.edit_requests.filter(status=TimeEditRequest.PENDING).first()

    def late_status(self, grace):
        if self.late_minutes is None:
            return ""
        if self.late_minutes > grace:
            return "late"
        if self.late_minutes < -grace:
            return "early"
        return "on time"


class TimeEditRequest(models.Model):
    PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
    STATUSES = [(PENDING, "Waiting for review"), (APPROVED, "Approved"), (REJECTED, "Declined")]

    entry = models.ForeignKey(TimeEntry, on_delete=models.PROTECT, related_name="edit_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    new_check_in = models.DateTimeField()
    new_check_out = models.DateTimeField()
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=STATUSES, default=PENDING)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class TaskPreset(models.Model):
    """A one-tap task button the owner can reuse."""
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="presets")
    title = models.CharField(max_length=120)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title


class Task(models.Model):
    ONCE, WEEKLY = "once", "weekly"
    RECURRENCES = [(ONCE, "One day only"), (WEEKLY, "Repeats weekly")]

    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="tasks")
    assigned_to = models.ForeignKey(StaffMember, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="tasks", help_text="Leave empty for whoever is on shift.")
    title = models.CharField(max_length=160)
    notes = models.TextField(blank=True)
    recurrence = models.CharField(max_length=10, choices=RECURRENCES, default=ONCE)
    due_date = models.DateField(null=True, blank=True)
    weekdays = models.CharField(max_length=20, blank=True, help_text="Comma-separated weekday numbers, Monday = 0.")
    due_time = models.TimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["due_time", "title"]

    def __str__(self):
        return self.title

    @property
    def weekday_list(self):
        return [int(d) for d in self.weekdays.split(",") if d.strip().isdigit()]

    def occurs_on(self, day):
        if self.recurrence == self.ONCE:
            return self.due_date == day
        return day.weekday() in self.weekday_list

    @property
    def schedule_label(self):
        if self.recurrence == self.ONCE:
            return self.due_date.strftime("%a %b %-d") if self.due_date else "No date"
        days = self.weekday_list
        if days == [0, 1, 2, 3, 4]:
            return "Weekdays"
        if len(days) == 7:
            return "Every day"
        return ", ".join(dict(WEEKDAYS)[d][:3] for d in days)


class TaskCompletion(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="completions")
    date = models.DateField()
    completed_by = models.ForeignKey(StaffMember, on_delete=models.PROTECT, related_name="completions")
    comment = models.CharField(max_length=500, blank=True)
    completed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = [("task", "date")]
        ordering = ["-completed_at"]


class Invitation(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField()
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    hourly_rate = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("20.00"))
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_usable(self):
        return not self.accepted_at and not self.revoked_at and self.expires_at > timezone.now()


class PayRecord(models.Model):
    """A snapshot of what was paid, kept even if rates or pay schedules change later."""
    staff = models.ForeignKey(StaffMember, on_delete=models.PROTECT, related_name="pay_records")
    period_start = models.DateField()
    period_end = models.DateField()
    pay_date = models.DateField()
    hours = models.DecimalField(max_digits=8, decimal_places=2)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    paid_at = models.DateTimeField(default=timezone.now)
    marked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-pay_date"]
        unique_together = [("staff", "pay_date")]


class AuditLog(models.Model):
    household = models.ForeignKey(Household, on_delete=models.CASCADE, related_name="audit_logs")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=60)
    details = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
