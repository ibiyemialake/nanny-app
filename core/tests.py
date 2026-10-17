from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (Household, Invitation, PayRecord, ScheduleSlot, StaffMember, Task, TaskCompletion, TaskPreset,
                     TimeEditRequest, TimeEntry)
from .services import flag_forgotten_checkouts, period_containing, summarize_period, task_items

User = get_user_model()


def aware(d, t):
    return timezone.make_aware(datetime.combine(d, t))


class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner@x.com", "owner@x.com", "pw-owner-123", first_name="Aishat")
        self.home = Household.objects.create(name="Test Home", owner=self.owner)
        self.nanny_user = User.objects.create_user("nanny@x.com", "nanny@x.com", "pw-nanny-123", first_name="Ada")
        self.nanny = StaffMember.objects.create(household=self.home, user=self.nanny_user, interac="ada@x.com")


class PayPeriodTests(Base):
    def test_paid_on_15th_and_last_day(self):
        self.assertEqual(period_containing(self.nanny, date(2026, 10, 1)), (date(2026, 10, 1), date(2026, 10, 15)))
        self.assertEqual(period_containing(self.nanny, date(2026, 10, 15)), (date(2026, 10, 1), date(2026, 10, 15)))
        self.assertEqual(period_containing(self.nanny, date(2026, 10, 16)), (date(2026, 10, 16), date(2026, 10, 31)))
        self.assertEqual(period_containing(self.nanny, date(2026, 11, 30)), (date(2026, 11, 16), date(2026, 11, 30)))

    def test_short_months_use_last_day(self):
        self.assertEqual(period_containing(self.nanny, date(2026, 2, 20)), (date(2026, 2, 16), date(2026, 2, 28)))
        self.assertEqual(period_containing(self.nanny, date(2028, 2, 29)), (date(2028, 2, 16), date(2028, 2, 29)))

    def test_summary_totals(self):
        d = date(2026, 10, 12)
        TimeEntry.objects.create(staff=self.nanny, check_in=aware(d, time(9)), check_out=aware(d, time(16, 30)),
                                 hourly_rate=Decimal("20"))
        TimeEntry.objects.create(staff=self.nanny, check_in=aware(d + timedelta(1), time(9)), hourly_rate=Decimal("20"))
        s = summarize_period(self.nanny, date(2026, 10, 1), date(2026, 10, 15))
        self.assertEqual(s.hours, Decimal("7.50"))
        self.assertEqual(s.amount, Decimal("150.00"))
        self.assertEqual(s.open_entries, 1)


class CheckInOutTests(Base):
    def setUp(self):
        super().setUp()
        self.client.login(username="nanny@x.com", password="pw-nanny-123")

    def test_check_in_and_out_with_note(self):
        self.client.post(reverse("check_in"))
        entry = TimeEntry.objects.get()
        self.assertTrue(entry.is_open)
        self.assertEqual(entry.hourly_rate, Decimal("20.00"))
        self.client.post(reverse("check_out"), {"checkout_note": "Great day"})
        entry.refresh_from_db()
        self.assertFalse(entry.is_open)
        self.assertEqual(entry.checkout_note, "Great day")

    def test_double_check_in_blocked(self):
        self.client.post(reverse("check_in"))
        self.client.post(reverse("check_in"))
        self.assertEqual(TimeEntry.objects.count(), 1)

    def test_late_arrival_recorded_and_emailed(self):
        self.home.notify_on_late = True
        self.home.save()
        now = timezone.localtime()
        start = (now - timedelta(minutes=30)).time().replace(second=0, microsecond=0)
        end = (now + timedelta(hours=2)).time().replace(second=0, microsecond=0)
        ScheduleSlot.objects.create(staff=self.nanny, weekday=now.weekday(), start_time=start, end_time=end)
        self.client.post(reverse("check_in"))
        entry = TimeEntry.objects.get()
        self.assertGreaterEqual(entry.late_minutes, 29)
        self.assertEqual(len(mail.outbox), 1)

    def test_nanny_cannot_see_owner_pages(self):
        self.assertEqual(self.client.get(reverse("timesheet")).status_code, 403)


class ForgottenCheckoutTests(Base):
    def test_flag_and_correction_flow(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        entry = TimeEntry.objects.create(staff=self.nanny, check_in=aware(yesterday, time(9)), hourly_rate=20,
                                         scheduled_start=time(9), scheduled_end=time(17))
        flag_forgotten_checkouts(self.home)
        entry.refresh_from_db()
        self.assertTrue(entry.flagged_forgotten)

        self.client.login(username="nanny@x.com", password="pw-nanny-123")
        self.assertContains(self.client.get("/"), "Did you forget to check out?")
        self.client.post(reverse("request_correction", args=[entry.pk]), {
            "new_check_in": f"{yesterday}T09:00", "new_check_out": f"{yesterday}T17:00", "reason": "Forgot"})
        req = TimeEditRequest.objects.get()
        # A pending fix no longer blocks today's check-in.
        self.client.post(reverse("check_in"))
        self.assertEqual(TimeEntry.objects.count(), 2)

        self.client.logout()
        self.client.login(username="owner@x.com", password="pw-owner-123")
        self.client.post(reverse("review_request", args=[req.pk]), {"decision": "approve"})
        entry.refresh_from_db()
        self.assertEqual(entry.duration_seconds, 8 * 3600)
        self.assertTrue(entry.edited)


class InviteTests(Base):
    def test_invite_and_signup(self):
        self.client.login(username="owner@x.com", password="pw-owner-123")
        self.client.post(reverse("staff_list"), {"email": "New@X.com", "hourly_rate": "22"})
        inv = Invitation.objects.get(email="new@x.com")
        self.assertEqual(len(mail.outbox), 1)
        self.client.logout()
        r = self.client.post(reverse("accept_invite", args=[inv.token]), {
            "first_name": "Bisi", "last_name": "", "interac": "(416) 555 0199",
            "password1": "long-enough-pw", "password2": "long-enough-pw"})
        self.assertRedirects(r, reverse("home"), fetch_redirect_response=False)
        staff = StaffMember.objects.get(user__email="new@x.com")
        self.assertEqual(staff.hourly_rate, Decimal("22.00"))
        self.assertEqual(staff.interac, "416-555-0199")
        inv.refresh_from_db()
        self.assertFalse(inv.is_usable)

    def test_deactivated_nanny_cannot_sign_in(self):
        self.client.login(username="owner@x.com", password="pw-owner-123")
        self.client.post(reverse("toggle_active", args=[self.nanny.pk]))
        self.client.logout()
        self.assertFalse(self.client.login(username="nanny@x.com", password="pw-nanny-123"))


@override_settings(STAFF_ALLOWED_NETWORKS=["192.168.1.0/24"])
class NetworkTests(Base):
    def test_nanny_blocked_off_network(self):
        r = self.client.post(reverse("login"), {"username": "nanny@x.com", "password": "pw-nanny-123"},
                             REMOTE_ADDR="8.8.8.8")
        self.assertContains(r, "home Wi-Fi")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_nanny_allowed_on_network(self):
        r = self.client.post(reverse("login"), {"username": "nanny@x.com", "password": "pw-nanny-123"},
                             REMOTE_ADDR="192.168.1.23")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/", REMOTE_ADDR="192.168.1.23").status_code, 200)
        # Leaves the house with the session still open: signed out.
        self.assertEqual(self.client.get("/", REMOTE_ADDR="8.8.8.8").status_code, 403)

    def test_owner_not_restricted(self):
        self.client.post(reverse("login"), {"username": "owner@x.com", "password": "pw-owner-123"}, REMOTE_ADDR="8.8.8.8")
        self.assertEqual(self.client.get("/", REMOTE_ADDR="8.8.8.8").status_code, 200)


class TaskTests(Base):
    def test_recurring_and_overdue(self):
        today = timezone.localdate()
        weekly = Task.objects.create(household=self.home, title="Snack", recurrence="weekly",
                                     weekdays=str(today.weekday()))
        old = Task.objects.create(household=self.home, title="Old", recurrence="once", due_date=today - timedelta(2))
        Task.objects.create(household=self.home, title="Not mine", recurrence="once", due_date=today,
                            assigned_to=StaffMember.objects.create(
                                household=self.home, user=User.objects.create_user("b@x.com", "b@x.com", "x")))
        titles = [i.task.title for i in task_items(self.home, today, self.nanny)]
        self.assertEqual(sorted(titles), ["Old", "Snack"])

        self.client.login(username="nanny@x.com", password="pw-nanny-123")
        self.client.post(reverse("complete_task"), {"task": weekly.pk, "date": today.isoformat(), "comment": "Apples"})
        self.assertEqual(TaskCompletion.objects.get().comment, "Apples")

    def test_preset_button(self):
        preset = TaskPreset.objects.create(household=self.home, title="Bath time")
        self.client.login(username="owner@x.com", password="pw-owner-123")
        self.client.post(reverse("quick_task"), {"quick-preset": preset.pk, "quick-recurrence": "once",
                                                 "quick-due_date": timezone.localdate().isoformat(),
                                                 "quick-assigned_to": self.nanny.pk})
        task = Task.objects.get()
        self.assertEqual(task.assigned_to, self.nanny)
        self.assertEqual(task.recurrence, Task.ONCE)

    def test_preset_button_every_day(self):
        preset = TaskPreset.objects.create(household=self.home, title="Tidy toys")
        self.client.login(username="owner@x.com", password="pw-owner-123")
        self.client.post(reverse("quick_task"), {"quick-preset": preset.pk, "quick-recurrence": "daily"})
        task = Task.objects.get()
        self.assertEqual(task.recurrence, Task.WEEKLY)
        self.assertEqual(task.weekday_list, [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(task.schedule_label, "Every day")

    def test_preset_button_chosen_days(self):
        preset = TaskPreset.objects.create(household=self.home, title="Laundry")
        self.client.login(username="owner@x.com", password="pw-owner-123")
        self.client.post(reverse("quick_task"), {"quick-preset": preset.pk, "quick-recurrence": "weekly"})
        self.assertFalse(Task.objects.exists())  # no days picked
        self.client.post(reverse("quick_task"), {"quick-preset": preset.pk, "quick-recurrence": "weekly",
                                                 "quick-weekdays": ["0", "3"]})
        self.assertEqual(Task.objects.get().weekday_list, [0, 3])

    def test_new_task_every_day(self):
        self.client.login(username="owner@x.com", password="pw-owner-123")
        self.client.post(reverse("tasks"), {"title": "Water plants", "recurrence": "daily"})
        self.assertEqual(Task.objects.get().schedule_label, "Every day")


class LateCheckoutTests(Base):
    def setUp(self):
        super().setUp()
        self.day = date(2026, 10, 14)
        self.entry = TimeEntry.objects.create(
            staff=self.nanny, check_in=aware(self.day, time(9)), hourly_rate=20,
            scheduled_start=time(9), scheduled_end=time(17))

    def check_out_at(self, t):
        self.client.login(username="nanny@x.com", password="pw-nanny-123")
        with mock.patch("django.utils.timezone.now", return_value=aware(self.day, t)):
            self.client.post(reverse("check_out"), {"checkout_note": ""})
        self.client.logout()
        self.entry.refresh_from_db()

    def review(self, **data):
        self.client.login(username="owner@x.com", password="pw-owner-123")
        r = self.client.post(reverse("review_late_checkout", args=[self.entry.pk]), data)
        self.entry.refresh_from_db()
        return r

    def test_within_grace_not_flagged(self):
        self.check_out_at(time(17, 5))
        self.assertEqual(self.entry.late_checkout_status, "")

    def test_late_checkout_flagged_and_shown(self):
        self.check_out_at(time(18, 30))
        self.assertTrue(self.entry.late_checkout_pending)
        self.assertEqual(self.entry.late_checkout_minutes, 90)
        self.client.login(username="owner@x.com", password="pw-owner-123")
        self.assertContains(self.client.get("/"), "after their scheduled end")
        self.assertContains(self.client.get(reverse("timesheet")), "Set finish time")

    def test_accept_keeps_time(self):
        self.check_out_at(time(18, 30))
        self.review(decision="accept")
        self.assertEqual(self.entry.late_checkout_status, "accepted")
        self.assertEqual(self.entry.check_out, aware(self.day, time(18, 30)))
        self.assertFalse(self.entry.edited)

    def test_decline_resets_to_scheduled_end(self):
        self.check_out_at(time(18, 30))
        self.review(decision="decline")
        self.assertEqual(self.entry.late_checkout_status, "declined")
        self.assertEqual(self.entry.check_out, aware(self.day, time(17)))
        self.assertEqual(self.entry.original_check_out, aware(self.day, time(18, 30)))
        self.assertEqual(self.entry.duration_seconds, 8 * 3600)
        self.assertTrue(self.entry.edited)

    def test_set_actual_finish_time(self):
        self.check_out_at(time(18, 30))
        self.review(decision="adjust", finished_at=f"{self.day}T17:45")
        self.assertEqual(self.entry.late_checkout_status, "adjusted")
        self.assertEqual(self.entry.check_out, aware(self.day, time(17, 45)))

    def test_finish_time_cannot_be_after_checkout(self):
        self.check_out_at(time(18, 30))
        self.review(decision="adjust", finished_at=f"{self.day}T19:00")
        self.assertTrue(self.entry.late_checkout_pending)
        self.assertEqual(self.entry.check_out, aware(self.day, time(18, 30)))

    def test_blocks_mark_paid_until_reviewed(self):
        self.check_out_at(time(18, 30))
        self.client.login(username="owner@x.com", password="pw-owner-123")
        self.client.post(reverse("mark_paid", args=[self.nanny.pk]), {"pay_date": "2026-10-15"})
        self.assertFalse(PayRecord.objects.exists())
        self.review(decision="decline")
        self.client.post(reverse("mark_paid", args=[self.nanny.pk]), {"pay_date": "2026-10-15"})
        self.assertEqual(PayRecord.objects.get().amount, Decimal("160.00"))


class InteracTests(Base):
    def test_nanny_edits_interac(self):
        self.client.login(username="nanny@x.com", password="pw-nanny-123")
        self.client.post(reverse("my_hours"), {"interac": "Ada.New@X.com"})
        self.nanny.refresh_from_db()
        self.assertEqual(self.nanny.interac, "ada.new@x.com")
        r = self.client.post(reverse("my_hours"), {"interac": "123"})
        self.assertContains(r, "10-digit phone number")

    def test_home_prompts_when_missing(self):
        self.nanny.interac = ""
        self.nanny.save()
        self.client.login(username="nanny@x.com", password="pw-nanny-123")
        self.assertContains(self.client.get("/"), "Add your Interac details")


class PaydayTests(Base):
    def test_mark_paid_snapshot(self):
        d = date(2026, 10, 15)
        TimeEntry.objects.create(staff=self.nanny, check_in=aware(d, time(0, 1)),
                                 check_out=aware(d, time(4, 1)), hourly_rate=20)
        with mock.patch("django.utils.timezone.localdate", return_value=d):
            self.client.login(username="nanny@x.com", password="pw-nanny-123")
            self.assertContains(self.client.get("/"), "It's payday")
            self.client.logout()
        self.client.login(username="owner@x.com", password="pw-owner-123")
        self.client.post(reverse("mark_paid", args=[self.nanny.pk]), {"pay_date": d.isoformat()})
        record = PayRecord.objects.get()
        self.assertEqual(record.amount, Decimal("80.00"))


class PaydayNoticeTests(Base):
    payday = date(2026, 10, 15)

    def setUp(self):
        super().setUp()
        TimeEntry.objects.create(staff=self.nanny, check_in=aware(self.payday, time(9)),
                                 check_out=aware(self.payday, time(13)), hourly_rate=20)

    def get(self, who, url="/", day=None):
        self.client.login(username=f"{who}@x.com", password=f"pw-{who}-123")
        with mock.patch("django.utils.timezone.localdate", return_value=day or self.payday):
            r = self.client.get(url)
        self.client.logout()
        return r

    def test_owner_sees_payday_and_pays(self):
        r = self.get("owner")
        self.assertContains(r, "It's payday")
        self.assertContains(r, "ada@x.com")  # Interac details
        self.assertContains(r, ">Paid</button>")
        self.client.login(username="owner@x.com", password="pw-owner-123")
        with mock.patch("django.utils.timezone.localdate", return_value=self.payday):
            r = self.client.post(reverse("mark_paid", args=[self.nanny.pk]),
                                 {"pay_date": self.payday.isoformat(), "next": "/"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertEqual(PayRecord.objects.get().amount, Decimal("80.00"))
        self.client.logout()
        self.assertNotContains(self.get("owner"), ">Paid</button>")

    def test_nanny_sees_payday_then_paid(self):
        r = self.get("nanny")
        self.assertContains(r, "It's payday")
        self.assertContains(r, "Not paid yet")
        PayRecord.objects.create(staff=self.nanny, period_start=date(2026, 10, 1), period_end=self.payday,
                                 pay_date=self.payday, hours=4, amount=80, marked_by=self.owner)
        r = self.get("nanny")
        self.assertContains(r, "paid you <strong class=\"num\">$80.00</strong>")
        self.assertContains(self.get("nanny", reverse("my_hours")), "paid")

    def test_overdue_unpaid_payday_still_shown_to_owner(self):
        r = self.get("owner", day=date(2026, 10, 17))
        self.assertContains(r, "Pay due")
        self.assertContains(r, "payday was Thu Oct 15")


class PagesRender(Base):
    def test_all_owner_pages(self):
        self.client.login(username="owner@x.com", password="pw-owner-123")
        for name, args in [("home", []), ("staff_list", []), ("staff_detail", [self.nanny.pk]), ("timesheet", []),
                           ("add_entry", []), ("tasks", []), ("activity", []), ("household_settings", [])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200, name)

    def test_all_staff_pages(self):
        self.client.login(username="nanny@x.com", password="pw-nanny-123")
        for name in ["home", "my_hours"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
