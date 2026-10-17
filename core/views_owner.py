"""Pages for the home owner."""
from datetime import date, time, timedelta

from django.contrib import messages
from django.core.mail import send_mail
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.conf import settings
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import (AddEntryForm, EntryForm, HouseholdSettingsForm, InviteForm, LateCheckoutForm, QuickTaskForm,
                    ReviewForm, ScheduleForm, StaffPayForm, TaskForm)
from .models import (WEEKDAYS, AuditLog, Invitation, PayRecord, ScheduleSlot, StaffMember, Task, TaskCompletion,
                     TaskPreset, TimeEditRequest, TimeEntry)
from .permissions import owner_required
from .services import (blocking_open_entry, current_summary, flag_forgotten_checkouts, fmt_dt, local_dt, log,
                       payday_summaries, period_containing, recent_periods, resolve_late_checkout, summarize_period,
                       task_items)


def safe_next(request, fallback):
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect(fallback)


def late_checkouts(household):
    return (TimeEntry.objects.filter(staff__household=household, late_checkout_status=TimeEntry.LATE_PENDING)
            .select_related("staff__user").order_by("check_in"))


def invite_link(invitation):
    return settings.SITE_URL + reverse("accept_invite", args=[invitation.token])


# --- Dashboard -----------------------------------------------------------------------

@owner_required
def dashboard(request):
    household = request.household
    flag_forgotten_checkouts(household)
    today = timezone.localdate()
    grace = household.late_grace_minutes

    people, paydays = [], []
    for staff in household.staff.filter(user__is_active=True).select_related("user"):
        paydays.extend(payday_summaries(staff, today))
        open_entry = blocking_open_entry(staff)
        today_entries = list(staff.entries.filter(check_in__date=today).order_by("check_in"))
        first = today_entries[0] if today_entries else None
        people.append({
            "staff": staff,
            "open_entry": open_entry,
            "slots": staff.schedule.filter(weekday=today.weekday()),
            "today_entries": today_entries,
            "arrival": first.late_status(grace) if first else "",
            "arrival_minutes": first.late_minutes if first else None,
            "summary": current_summary(staff),
        })

    forgotten = (TimeEntry.objects.filter(staff__household=household, check_out__isnull=True, flagged_forgotten=True)
                 .exclude(edit_requests__status=TimeEditRequest.PENDING).select_related("staff__user"))
    pending = (TimeEditRequest.objects.filter(entry__staff__household=household, status=TimeEditRequest.PENDING)
               .select_related("entry__staff__user"))
    notes = (TimeEntry.objects.filter(staff__household=household).exclude(checkout_note="")
             .select_related("staff__user").order_by("-check_out")[:6])
    return render(request, "core/owner/dashboard.html", {
        "today": today,
        "people": people,
        "paydays": sorted(paydays, key=lambda s: (s.pay_record is not None, s.end, s.staff.name)),
        "payday_today": any(s.is_payday for s in paydays),
        "forgotten": forgotten,
        "pending": pending,
        "late_checkouts": late_checkouts(household),
        "review_form": ReviewForm(),
        "notes": notes,
        "tasks": task_items(household, today),
        "has_staff": household.staff.exists(),
    })


# --- Staff & invitations ---------------------------------------------------------------

@owner_required
def staff_list(request):
    household = request.household
    form = InviteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        if household.staff.filter(user__email=email).exists() or email == request.user.email:
            form.add_error("email", "This person already has an account.")
        else:
            household.invitations.filter(email=email, accepted_at__isnull=True,
                                         revoked_at__isnull=True).update(revoked_at=timezone.now())
            invitation = Invitation.objects.create(
                household=household, email=email, hourly_rate=form.cleaned_data["hourly_rate"],
                expires_at=timezone.now() + timedelta(days=7))
            link = invite_link(invitation)
            send_mail(
                f"You're invited to {household.name}",
                f"Hi,\n\n{request.user.get_full_name() or 'Your employer'} has invited you to check in and see "
                f"your tasks and hours for {household.name}.\n\nOpen this link while you're connected to the "
                f"home Wi-Fi to create your account:\n{link}\n\nThe link works for 7 days.",
                None, [email], fail_silently=True)
            log(household, request.user, "Invite sent", email)
            messages.success(request, f"Invite sent to {email}. You can also copy the link below and send it yourself.")
            return redirect("staff_list")
    invitations = [(inv, invite_link(inv)) for inv in household.invitations.all() if inv.is_usable]
    return render(request, "core/owner/staff_list.html", {
        "form": form,
        "active": household.staff.filter(user__is_active=True).select_related("user"),
        "inactive": household.staff.filter(user__is_active=False).select_related("user"),
        "invitations": invitations,
    })


@owner_required
@require_POST
def revoke_invite(request, pk):
    invitation = get_object_or_404(Invitation, pk=pk, household=request.household)
    invitation.revoked_at = timezone.now()
    invitation.save(update_fields=["revoked_at"])
    log(request.household, request.user, "Invite cancelled", invitation.email)
    messages.info(request, f"Cancelled the invite for {invitation.email}.")
    return redirect("staff_list")


@owner_required
def staff_detail(request, pk):
    household = request.household
    staff = get_object_or_404(StaffMember.objects.select_related("user"), pk=pk, household=household)
    pay_form = StaffPayForm(instance=staff, prefix="pay")
    schedule_form = ScheduleForm(prefix="sched")

    if request.method == "POST" and "save_pay" in request.POST:
        before = staff.hourly_rate
        pay_form = StaffPayForm(request.POST, instance=staff, prefix="pay")
        if pay_form.is_valid():
            staff = pay_form.save()
            if before != staff.hourly_rate:
                log(household, request.user, "Pay settings changed",
                    f"{staff.name}: ${before}/h → ${staff.hourly_rate}/h.")
            messages.success(request, "Pay rate saved. A new rate applies to shifts from now on.")
            return redirect("staff_detail", pk=staff.pk)

    if request.method == "POST" and "add_slot" in request.POST:
        schedule_form = ScheduleForm(request.POST, prefix="sched")
        if schedule_form.is_valid():
            data = schedule_form.cleaned_data
            for day in data["weekdays"]:
                ScheduleSlot.objects.create(staff=staff, weekday=int(day),
                                            start_time=data["start_time"], end_time=data["end_time"])
            names = ", ".join(dict(WEEKDAYS)[int(d)] for d in data["weekdays"])
            log(household, request.user, "Schedule changed",
                f"{staff.name}: added {names} {data['start_time']:%-I:%M %p}–{data['end_time']:%-I:%M %p}.")
            messages.success(request, "Schedule updated.")
            return redirect("staff_detail", pk=staff.pk)

    periods = [summarize_period(staff, start, end) for start, end in recent_periods(staff, 4)]
    return render(request, "core/owner/staff_detail.html", {
        "staff": staff,
        "pay_form": pay_form,
        "schedule_form": schedule_form,
        "slots": staff.schedule.all(),
        "current": periods[0],
        "past": periods[1:],
        "pay_records": staff.pay_records.all()[:12],
    })


@owner_required
@require_POST
def delete_slot(request, pk):
    slot = get_object_or_404(ScheduleSlot, pk=pk, staff__household=request.household)
    staff = slot.staff
    log(request.household, request.user, "Schedule changed",
        f"{staff.name}: removed {slot.get_weekday_display()} {slot.start_time:%-I:%M %p}–{slot.end_time:%-I:%M %p}.")
    slot.delete()
    return redirect("staff_detail", pk=staff.pk)


@owner_required
@require_POST
def toggle_active(request, pk):
    staff = get_object_or_404(StaffMember, pk=pk, household=request.household)
    user = staff.user
    user.is_active = not user.is_active
    user.save(update_fields=["is_active"])
    staff.deactivated_at = None if user.is_active else timezone.now()
    staff.save(update_fields=["deactivated_at"])
    if user.is_active:
        log(request.household, request.user, "Access restored", staff.name)
        messages.success(request, f"{staff.name} can sign in again.")
    else:
        open_entry = staff.entries.filter(check_out__isnull=True).first()
        log(request.household, request.user, "Access removed", staff.name)
        messages.success(request, f"{staff.name} can no longer sign in. Their hours and notes are kept.")
        if open_entry:
            messages.warning(request, "They still have an open shift. Add a check-out time on the Hours page.")
    return redirect("staff_detail", pk=staff.pk)


@owner_required
@require_POST
def mark_paid(request, pk):
    staff = get_object_or_404(StaffMember, pk=pk, household=request.household)
    back = reverse("staff_detail", args=[pk])
    try:
        pay_date = date.fromisoformat(request.POST.get("pay_date", ""))
    except ValueError:
        return safe_next(request, back)
    start, end = period_containing(staff, pay_date)
    if end != pay_date or pay_date > timezone.localdate():
        messages.error(request, "You can only mark a payday that has arrived.")
        return safe_next(request, back)
    summary = summarize_period(staff, start, end)
    if summary.open_entries:
        messages.error(request, "This pay period has a shift with no check-out. Fix it on the Hours page first.")
        return safe_next(request, back)
    if summary.late_checkouts:
        messages.error(request, "This pay period has a late check-out to review. Accept, decline or change it first.")
        return safe_next(request, back)
    _, created = PayRecord.objects.get_or_create(
        staff=staff, pay_date=end,
        defaults={"period_start": start, "period_end": end, "hours": summary.hours,
                  "amount": summary.amount, "marked_by": request.user})
    if created:
        log(request.household, request.user, "Marked as paid",
            f"{staff.name}: {summary.hours} h, ${summary.amount} for {start:%b %-d}–{end:%b %-d}.")
        messages.success(request, f"Marked ${summary.amount} as paid to {staff.first_name}. "
                                  f"They'll see it on their page.")
    return safe_next(request, back)


@owner_required
@require_POST
def unmark_paid(request, pk):
    record = get_object_or_404(PayRecord, pk=pk, staff__household=request.household)
    staff = record.staff
    log(request.household, request.user, "Payment undone",
        f"{staff.name}: ${record.amount} for {record.period_start:%b %-d}–{record.period_end:%b %-d}.")
    record.delete()
    messages.info(request, "Payment record removed. This is noted in the activity log.")
    return redirect("staff_detail", pk=staff.pk)


# --- Hours -----------------------------------------------------------------------

@owner_required
def timesheet(request):
    household = request.household
    flag_forgotten_checkouts(household)
    staff_qs = household.staff.select_related("user")
    entries = TimeEntry.objects.filter(staff__household=household).select_related("staff__user")
    selected = request.GET.get("staff")
    if selected and selected.isdigit():
        entries = entries.filter(staff_id=int(selected))
    days = request.GET.get("days", "30")
    days = int(days) if days.isdigit() else 30
    since = timezone.localdate() - timedelta(days=days)
    entries = entries.filter(check_in__gte=local_dt(since, time.min))
    return render(request, "core/owner/timesheet.html", {
        "entries": entries,
        "staff_qs": staff_qs,
        "selected": selected,
        "days": str(days),
        "pending": TimeEditRequest.objects.filter(entry__staff__household=household,
                                                  status=TimeEditRequest.PENDING).select_related("entry__staff__user"),
        "late_checkouts": late_checkouts(household),
        "review_form": ReviewForm(),
        "grace": household.late_grace_minutes,
    })


def _describe_entry(entry):
    return (f"in {fmt_dt(entry.check_in)}, out {fmt_dt(entry.check_out)}, ${entry.hourly_rate}/h")


@owner_required
def edit_entry(request, pk):
    entry = get_object_or_404(TimeEntry.objects.select_related("staff__user"), pk=pk, staff__household=request.household)
    initial = {"check_in": entry.check_in, "check_out": entry.check_out,
               "hourly_rate": entry.hourly_rate, "checkout_note": entry.checkout_note}
    form = EntryForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        before = _describe_entry(entry)
        for name in ("check_in", "check_out", "hourly_rate", "checkout_note"):
            setattr(entry, name, form.cleaned_data[name])
        entry.edited = True
        if entry.late_checkout_pending:
            entry.late_checkout_status = TimeEntry.LATE_ADJUSTED  # editing the shift settles the review
        entry.save()
        log(request.household, request.user, "Shift edited",
            f"{entry.staff.name}: {before} → {_describe_entry(entry)}.")
        messages.success(request, "Shift updated.")
        return redirect("timesheet")
    return render(request, "core/owner/entry_form.html", {"form": form, "entry": entry})


@owner_required
def add_entry(request):
    form = AddEntryForm(request.POST or None, household=request.household)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        entry = TimeEntry.objects.create(
            staff=data["staff"], check_in=data["check_in"], check_out=data["check_out"],
            hourly_rate=data["hourly_rate"] if data["hourly_rate"] is not None else data["staff"].hourly_rate,
            checkout_note=data["checkout_note"], edited=True)
        log(request.household, request.user, "Shift added by hand", f"{entry.staff.name}: {_describe_entry(entry)}.")
        messages.success(request, "Shift added.")
        return redirect("timesheet")
    return render(request, "core/owner/entry_form.html", {"form": form, "entry": None})


@owner_required
@require_POST
def review_request(request, pk):
    edit = get_object_or_404(TimeEditRequest.objects.select_related("entry__staff__user"), pk=pk,
                             entry__staff__household=request.household, status=TimeEditRequest.PENDING)
    form = ReviewForm(request.POST)
    if not form.is_valid():
        return safe_next(request, "timesheet")
    entry = edit.entry
    edit.reviewed_by = request.user
    edit.reviewed_at = timezone.now()
    edit.review_note = form.cleaned_data["review_note"]
    with transaction.atomic():
        if form.cleaned_data["decision"] == "approve":
            before = _describe_entry(entry)
            entry.check_in, entry.check_out, entry.edited = edit.new_check_in, edit.new_check_out, True
            entry.save()
            edit.status = TimeEditRequest.APPROVED
            log(request.household, request.user, "Time fix approved",
                f"{entry.staff.name}: {before} → {_describe_entry(entry)}.")
            messages.success(request, f"Approved. {entry.staff.first_name}'s hours are updated.")
        else:
            edit.status = TimeEditRequest.REJECTED
            log(request.household, request.user, "Time fix declined",
                f"{entry.staff.name}: asked for {fmt_dt(edit.new_check_in)} to {fmt_dt(edit.new_check_out)}.")
            if entry.check_out is None:
                messages.warning(request, "Declined. That shift still has no check-out, so set the right time here.")
                edit.save()
                return redirect("edit_entry", pk=entry.pk)
            messages.info(request, "Declined. The shift is unchanged.")
        edit.save()
    return safe_next(request, "timesheet")


@owner_required
@require_POST
def review_late_checkout(request, pk):
    entry = get_object_or_404(TimeEntry.objects.select_related("staff__user"), pk=pk,
                              staff__household=request.household, late_checkout_status=TimeEntry.LATE_PENDING)
    form = LateCheckoutForm(request.POST, entry=entry)
    if not form.is_valid():
        errors = form.non_field_errors() or [e for errs in form.errors.values() for e in errs]
        messages.error(request, errors[0] if errors else "Couldn't save that. Try again.")
        return safe_next(request, "timesheet")
    decision = form.cleaned_data["decision"]
    resolve_late_checkout(entry, request.user, decision, form.cleaned_data["finished_at"])
    name = entry.staff.first_name
    if decision == "accept":
        messages.success(request, f"Accepted. {name}'s check-out stays at {fmt_dt(entry.check_out)}.")
    elif decision == "decline":
        messages.success(request, f"Declined. {name}'s shift now ends at the scheduled time, {fmt_dt(entry.check_out)}.")
    else:
        messages.success(request, f"Updated. {name}'s shift now ends at {fmt_dt(entry.check_out)}.")
    return safe_next(request, "timesheet")


# --- Tasks -----------------------------------------------------------------------

def create_task(household, data, **fields):
    """Create a task from cleaned "who / when" form data."""
    weekly = data["recurrence"] == Task.WEEKLY
    return Task.objects.create(
        household=household, assigned_to=data["assigned_to"], recurrence=data["recurrence"],
        due_date=None if weekly else data["due_date"],
        weekdays=",".join(sorted(data["weekdays"])) if weekly else "", **fields)


@owner_required
def tasks(request):
    household = request.household
    today = timezone.localdate()
    form = TaskForm(request.POST or None, household=household, initial={"due_date": today})
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        task = create_task(household, data, title=data["title"], notes=data["notes"], due_time=data["due_time"])
        messages.success(request, f"Added “{task.title}”.")
        return redirect("tasks")
    recent = (TaskCompletion.objects.filter(task__household=household, date__gte=today - timedelta(days=7))
              .select_related("task", "completed_by__user"))
    return render(request, "core/owner/tasks.html", {
        "form": form,
        "quick_form": QuickTaskForm(household=household, initial={"due_date": today}, prefix="quick"),
        "presets": household.presets.all(),
        "recurring": household.tasks.filter(is_active=True, recurrence=Task.WEEKLY).select_related("assigned_to__user"),
        "upcoming": household.tasks.filter(is_active=True, recurrence=Task.ONCE, due_date__gte=today)
                    .order_by("due_date", "due_time").select_related("assigned_to__user"),
        "today_items": task_items(household, today),
        "recent": recent,
    })


@owner_required
@require_POST
def quick_task(request):
    household = request.household
    form = QuickTaskForm(request.POST, household=household, prefix="quick")
    if form.is_valid():
        preset = get_object_or_404(TaskPreset, pk=form.cleaned_data["preset"], household=household)
        task = create_task(household, form.cleaned_data, title=preset.title)
        messages.success(request, f"Added “{preset.title}” ({task.schedule_label.lower()}).")
    else:
        errors = [e for field in ("due_date", "weekdays") for e in form.errors.get(field, [])]
        messages.error(request, errors[0] if errors else "Couldn't add that task. Try again.")
    return redirect("tasks")


@owner_required
@require_POST
def archive_task(request, pk):
    task = get_object_or_404(Task, pk=pk, household=request.household)
    task.is_active = False
    task.save(update_fields=["is_active"])
    messages.info(request, f"Removed “{task.title}”. Past completions are kept.")
    return redirect("tasks")


@owner_required
@require_POST
def add_preset(request):
    title = request.POST.get("title", "").strip()[:120]
    if title:
        TaskPreset.objects.get_or_create(household=request.household, title=title)
    return redirect("tasks")


@owner_required
@require_POST
def delete_preset(request, pk):
    get_object_or_404(TaskPreset, pk=pk, household=request.household).delete()
    return redirect("tasks")


# --- Activity & settings ------------------------------------------------------------

@owner_required
def activity(request):
    return render(request, "core/owner/activity.html", {
        "logs": AuditLog.objects.filter(household=request.household).select_related("actor")[:300],
    })


@owner_required
def household_settings(request):
    form = HouseholdSettingsForm(request.POST or None, instance=request.household)
    if request.method == "POST" and form.is_valid():
        form.save()
        log(request.household, request.user, "Settings changed")
        messages.success(request, "Settings saved.")
        return redirect("household_settings")
    return render(request, "core/owner/settings.html", {
        "form": form,
        "networks": settings.STAFF_ALLOWED_NETWORKS,
        "client_ip": request.client_ip,
        "on_home_network": request.on_home_network,
        "site_url": settings.SITE_URL,
    })
