"""Pages for nannies."""
from datetime import date, timedelta

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import CorrectionForm, HandoverForm, InteracForm
from .models import Task, TaskCompletion, TimeEditRequest, TimeEntry
from .permissions import staff_required
from .services import (blocking_open_entry, current_summary, flag_forgotten_checkouts, fmt_dt, fmt_time, local_dt, log,
                       is_late_checkout, notify_owner, recent_periods, slot_for, summarize_period, task_items)
from .templatetags.home_extras import minutes_label


def is_checked_in(staff):
    """Checked in to a current shift. A shift flagged as a missed check-out doesn't count."""
    entry = blocking_open_entry(staff)
    return bool(entry and not entry.flagged_forgotten)


@staff_required
def staff_home(request):
    staff = request.staff
    flag_forgotten_checkouts(staff.household)
    today = timezone.localdate()
    open_entry = blocking_open_entry(staff)
    summary = current_summary(staff)
    last_paid = staff.pay_records.first()
    # After payday has passed, keep showing the payment for a few days.
    recently_paid = (last_paid if last_paid and last_paid != summary.pay_record
                     and timezone.localtime(last_paid.paid_at).date() >= today - timedelta(days=3) else None)
    return render(request, "core/staff/home.html", {
        "staff": staff,
        "open_entry": open_entry,
        "today": today,
        "todays_slots": staff.schedule.filter(weekday=today.weekday()),
        "today_entries": staff.entries.filter(check_in__date=today, check_out__isnull=False).order_by("check_in"),
        "tasks": task_items(staff.household, today, staff),
        "can_tick_tasks": bool(open_entry and not open_entry.flagged_forgotten),
        "summary": summary,
        "last_paid": last_paid,
        "recently_paid": recently_paid,
        "pending_requests": TimeEditRequest.objects.filter(entry__staff=staff, status=TimeEditRequest.PENDING).count(),
    })


@staff_required
@require_POST
def check_in(request):
    staff, household = request.staff, request.household
    if blocking_open_entry(staff):
        messages.info(request, "You're already checked in.")
        return redirect("home")
    now = timezone.now()
    local_now = timezone.localtime(now)
    slot = slot_for(staff, local_now)
    late, paid_from = None, now
    if slot:
        scheduled = local_dt(local_now.date(), slot.start_time)
        earliest = scheduled - timedelta(minutes=household.late_grace_minutes)
        if now < earliest:
            messages.error(request, f"Your shift starts at {fmt_time(scheduled)}. "
                                    f"You can check in from {fmt_time(earliest)}.")
            return redirect("home")
        late = round((now - scheduled).total_seconds() / 60)
        paid_from = max(now, scheduled)  # hours count from the scheduled start, not before
    entry = TimeEntry.objects.create(
        staff=staff, check_in=paid_from, original_check_in=now if paid_from != now else None,
        hourly_rate=staff.hourly_rate,
        scheduled_start=slot.start_time if slot else None, scheduled_end=slot.end_time if slot else None,
        unscheduled_status="" if slot else TimeEntry.UNSCHEDULED_PENDING,
        late_minutes=late, created_ip=request.client_ip or None)
    detail = f"{staff.name} checked in at {fmt_time(now)}"
    if late is not None:
        detail += f" ({minutes_label(late)})"
    if paid_from != now:
        detail += f". Hours count from {fmt_time(paid_from)}"
    if not slot:
        detail += ". No shift was scheduled, so the hours need approval"
    log(household, request.user, "Checked in", detail + ".")
    if late is not None and late > household.late_grace_minutes and household.notify_on_late:
        notify_owner(household, f"{staff.first_name} checked in {minutes_label(late)}",
                     f"{detail}. Scheduled start was {slot.start_time.strftime('%-I:%M %p')}.")
    messages.success(request, f"Checked in at {fmt_time(now)}.")
    if paid_from != now:
        messages.info(request, f"You're a little early. Your hours start counting at {fmt_time(paid_from)}.")
    if not slot:
        messages.info(request, "You don't have a shift scheduled today, so these hours will need approval.")
    return redirect("home")


@staff_required
def check_out(request):
    staff = request.staff
    entry = blocking_open_entry(staff)
    if not entry:
        messages.info(request, "You're not checked in.")
        return redirect("home")
    form = HandoverForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        entry.check_out = timezone.now()
        entry.checkout_note = form.cleaned_data["checkout_note"].strip()[:5000]
        late = is_late_checkout(entry, request.household)
        if late:
            entry.late_checkout_status = TimeEntry.LATE_PENDING
            entry.original_check_out = entry.check_out
        with transaction.atomic():
            entry.save(update_fields=["check_out", "checkout_note", "late_checkout_status", "original_check_out"])
            handover = form.save(commit=False)
            handover.entry = entry
            handover.save()
        detail = f"{staff.name} checked out at {fmt_time(entry.check_out)}"
        if late:
            detail += f" ({minutes_label(entry.late_checkout_minutes)} after the scheduled end)"
        log(request.household, request.user, "Checked out",
            f"{detail}. Handover: {handover.total_feed_ml} ml fed, {handover.total_naps} nap(s), "
            f"{handover.total_diapers} diaper change(s), mood {handover.mood_word.lower()}"
            + (", with a note." if entry.checkout_note else "."))
        messages.success(request, f"Checked out at {fmt_time(entry.check_out)}. See you next time.")
        if late:
            messages.info(request, "That's later than your scheduled finish, so the extra time will be reviewed "
                                   "before payday.")
        return redirect("home")
    return render(request, "core/staff/check_out.html", {
        "entry": entry,
        "form": form,
        "tasks": task_items(staff.household, timezone.localdate(), staff),
        "can_tick_tasks": True,
    })


@staff_required
def request_correction(request, entry_id):
    staff = request.staff
    entry = get_object_or_404(TimeEntry, pk=entry_id, staff=staff)
    if entry.pending_request:
        messages.info(request, "You've already asked for a fix to this shift. It's waiting for review.")
        return redirect("my_hours")
    initial = {"new_check_in": entry.check_in, "new_check_out": entry.check_out}
    form = CorrectionForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        TimeEditRequest.objects.create(
            entry=entry, requested_by=request.user, new_check_in=data["new_check_in"],
            new_check_out=data["new_check_out"], reason=data["reason"])
        log(request.household, request.user, "Time fix requested",
            f"{staff.name}: {fmt_dt(data['new_check_in'])} to {fmt_dt(data['new_check_out'])}. “{data['reason']}”")
        messages.success(request, "Sent. Your hours will update once it's approved.")
        return redirect("home")
    return render(request, "core/staff/correction.html", {"form": form, "entry": entry})


def back(request):
    """Return to the check-out page if that's where the tap came from, otherwise home."""
    return redirect("check_out" if request.POST.get("next") == reverse("check_out") else "home")


@staff_required
@require_POST
def complete_task(request):
    staff = request.staff
    if not is_checked_in(staff):
        messages.error(request, "Check in first, then you can tick off tasks.")
        return redirect("home")
    task = get_object_or_404(Task, pk=request.POST.get("task"), household=request.household, is_active=True)
    if task.assigned_to_id not in (None, staff.id):
        messages.error(request, "That task belongs to someone else.")
        return redirect("home")
    try:
        day = date.fromisoformat(request.POST.get("date", ""))
    except ValueError:
        return redirect("home")
    today = timezone.localdate()
    if not (task.occurs_on(day) and day <= today):
        messages.error(request, "That task isn't due on that day.")
        return redirect("home")
    TaskCompletion.objects.update_or_create(
        task=task, date=day,
        defaults={"completed_by": staff, "comment": request.POST.get("comment", "").strip()[:500],
                  "completed_at": timezone.now()})
    messages.success(request, f"Done: {task.title}")
    return back(request)


@staff_required
@require_POST
def undo_task(request):
    if not is_checked_in(request.staff):
        messages.error(request, "Check in first, then you can change tasks.")
        return redirect("home")
    completion = get_object_or_404(TaskCompletion, pk=request.POST.get("completion"), completed_by=request.staff)
    if completion.date == timezone.localdate() or completion.task.recurrence == Task.ONCE:
        completion.delete()
        messages.info(request, f"Marked “{completion.task.title}” as not done.")
    return back(request)


@staff_required
def my_hours(request):
    staff = request.staff
    interac_form = InteracForm(request.POST or None, initial={"interac": staff.interac})
    if request.method == "POST" and interac_form.is_valid():
        before, staff.interac = staff.interac, interac_form.cleaned_data["interac"]
        if before != staff.interac:
            staff.save(update_fields=["interac"])
            log(request.household, request.user, "Interac details changed",
                f"{staff.name}: {before or '(none)'} → {staff.interac}.")
        messages.success(request, "Payment details saved.")
        return redirect("my_hours")
    periods = [summarize_period(staff, start, end) for start, end in recent_periods(staff, 6)]
    return render(request, "core/staff/hours.html", {
        "staff": staff,
        "interac_form": interac_form,
        "current": periods[0],
        "past": periods[1:],
        "requests": TimeEditRequest.objects.filter(entry__staff=staff).select_related("entry")[:10],
    })
