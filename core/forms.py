import re
from datetime import timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from .models import WEEKDAYS, Household, StaffMember, Task
from .permissions import get_role

User = get_user_model()
WEEKDAY_CHOICES = [(str(n), name) for n, name in WEEKDAYS]
DT_FORMAT = "%Y-%m-%dT%H:%M"


class NoColon:
    """Labels without a trailing colon."""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)


def dt_field(label, required=True, help_text=""):
    return forms.DateTimeField(
        label=label, required=required, help_text=help_text, input_formats=[DT_FORMAT],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format=DT_FORMAT))


def check_shift_times(cleaned, start_key, end_key, allow_open=False):
    start, end = cleaned.get(start_key), cleaned.get(end_key)
    if not start:
        return
    if end is None:
        if not allow_open:
            raise ValidationError("Add a check-out time.")
        return
    if end <= start:
        raise ValidationError("Check-out has to be after check-in.")
    if end - start > timedelta(hours=24):
        raise ValidationError("A single shift can't be longer than 24 hours.")
    if end > timezone.now() + timedelta(minutes=5):
        raise ValidationError("Check-out can't be in the future.")


# --- Accounts ---------------------------------------------------------------------

class LoginForm(NoColon, AuthenticationForm):
    username = forms.EmailField(label="Email", widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}))

    def clean_username(self):
        return self.cleaned_data["username"].strip().lower()

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if get_role(user) == "staff" and not getattr(self.request, "on_home_network", True):
            raise ValidationError("You can only sign in while connected to the home Wi-Fi.", code="network")


class _PasswordForm(NoColon, forms.Form):
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
                                help_text="At least 8 characters.")
    password2 = forms.CharField(label="Type it again", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "The passwords don't match.")
        elif p1:
            try:
                validate_password(p1)
            except ValidationError as error:
                self.add_error("password1", error)
        return cleaned


class SetupForm(_PasswordForm):
    household_name = forms.CharField(label="Home name", max_length=120, help_text="For example, the Ade residence.")
    your_name = forms.CharField(label="Your name", max_length=150)
    email = forms.EmailField(label="Your email")
    field_order = ["household_name", "your_name", "email", "password1", "password2"]

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(username=email).exists():
            raise ValidationError("An account with this email already exists.")
        return email


def interac_field():
    return forms.CharField(
        label="Interac e-Transfer email or phone", max_length=254,
        help_text="Where your pay is sent. You can change this later on your Hours & pay page.")


def clean_interac_value(value):
    """Accept an email address or a 10-digit phone number (optionally with a leading 1)."""
    value = value.strip()
    if "@" in value:
        validate_email(value)
        return value.lower()
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValidationError("Enter an email address or a 10-digit phone number.")
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


class StaffSignupForm(_PasswordForm):
    first_name = forms.CharField(label="First name", max_length=150)
    last_name = forms.CharField(label="Last name", max_length=150, required=False)
    interac = interac_field()
    field_order = ["first_name", "last_name", "interac", "password1", "password2"]

    def clean_interac(self):
        return clean_interac_value(self.cleaned_data["interac"])


class InteracForm(NoColon, forms.Form):
    interac = interac_field()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["interac"].help_text = "Where your pay is sent by Interac e-Transfer."

    def clean_interac(self):
        return clean_interac_value(self.cleaned_data["interac"])


class InviteForm(NoColon, forms.Form):
    email = forms.EmailField(label="Their email")
    hourly_rate = forms.DecimalField(label="Hourly rate ($)", min_value=0, max_digits=7, decimal_places=2, initial=20)

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()


# --- Staff settings ------------------------------------------------------------------

class StaffPayForm(NoColon, forms.ModelForm):
    class Meta:
        model = StaffMember
        fields = ["hourly_rate"]
        labels = {"hourly_rate": "Hourly rate ($)"}


class ScheduleForm(NoColon, forms.Form):
    weekdays = forms.MultipleChoiceField(label="Days", choices=WEEKDAY_CHOICES, widget=forms.CheckboxSelectMultiple)
    start_time = forms.TimeField(label="Starts", widget=forms.TimeInput(attrs={"type": "time"}))
    end_time = forms.TimeField(label="Ends", widget=forms.TimeInput(attrs={"type": "time"}),
                               help_text="An end time earlier than the start means an overnight shift.")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("start_time") and cleaned.get("start_time") == cleaned.get("end_time"):
            raise ValidationError("Start and end can't be the same time.")
        return cleaned


# --- Time entries -------------------------------------------------------------------

class EntryForm(NoColon, forms.Form):
    check_in = dt_field("Checked in")
    check_out = dt_field("Checked out", required=False, help_text="Leave empty if they're still here.")
    hourly_rate = forms.DecimalField(label="Rate for this shift ($)", min_value=0, max_digits=7, decimal_places=2)
    checkout_note = forms.CharField(label="Note", required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def clean(self):
        cleaned = super().clean()
        check_shift_times(cleaned, "check_in", "check_out", allow_open=True)
        return cleaned


class AddEntryForm(EntryForm):
    staff = forms.ModelChoiceField(label="Who", queryset=StaffMember.objects.none(), empty_label=None)
    field_order = ["staff", "check_in", "check_out", "hourly_rate", "checkout_note"]

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["staff"].queryset = StaffMember.objects.filter(household=household).select_related("user")
        self.fields["hourly_rate"].required = False
        self.fields["hourly_rate"].help_text = "Leave empty to use their current rate."


class CorrectionForm(NoColon, forms.Form):
    new_check_in = dt_field("I arrived at")
    new_check_out = dt_field("I left at")
    reason = forms.CharField(label="What happened?", widget=forms.Textarea(attrs={"rows": 2}),
                             help_text="For example: forgot to check out, phone died.")

    def clean(self):
        cleaned = super().clean()
        check_shift_times(cleaned, "new_check_in", "new_check_out")
        return cleaned


class ReviewForm(NoColon, forms.Form):
    decision = forms.ChoiceField(choices=[("approve", "Approve"), ("reject", "Decline")])
    review_note = forms.CharField(required=False, max_length=500)


class LateCheckoutForm(NoColon, forms.Form):
    decision = forms.ChoiceField(choices=[("accept", "Accept"), ("decline", "Decline"), ("adjust", "Set finish time")])
    finished_at = dt_field("They finished at", required=False)

    def __init__(self, *args, entry=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.entry = entry

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("decision") != "adjust":
            return cleaned
        finished = cleaned.get("finished_at")
        if not finished:
            raise ValidationError("Enter the time they finished work.")
        if finished <= self.entry.check_in:
            raise ValidationError("The finish time has to be after they checked in.")
        if finished > self.entry.original_check_out:
            raise ValidationError("The finish time can't be later than when they checked out.")
        return cleaned


# --- Tasks --------------------------------------------------------------------------

DAILY = "daily"
TASK_WHEN_CHOICES = [(Task.ONCE, "One day only"), (DAILY, "Every day"), (Task.WEEKLY, "Repeats on chosen days")]


def clean_task_when(form, cleaned):
    """Check the date or days for the chosen repeat, and store "every day" as weekly on all seven days."""
    recurrence = cleaned.get("recurrence")
    if recurrence == Task.ONCE and not cleaned.get("due_date"):
        form.add_error("due_date", "Pick the day this needs doing.")
    elif recurrence == DAILY:
        cleaned["recurrence"], cleaned["weekdays"] = Task.WEEKLY, [str(n) for n, _ in WEEKDAYS]
    elif recurrence == Task.WEEKLY and not cleaned.get("weekdays"):
        form.add_error("weekdays", "Pick at least one day.")
    return cleaned


def when_fields():
    return {
        "recurrence": forms.ChoiceField(label="When", choices=TASK_WHEN_CHOICES, initial=Task.ONCE,
                                        widget=forms.RadioSelect),
        "due_date": forms.DateField(label="Date", required=False, widget=forms.DateInput(attrs={"type": "date"})),
        "weekdays": forms.MultipleChoiceField(label="Days", required=False, choices=WEEKDAY_CHOICES,
                                              widget=forms.CheckboxSelectMultiple),
    }


def staff_choices_field(household, label="For"):
    return forms.ModelChoiceField(
        label=label, required=False, empty_label="Whoever is on shift",
        queryset=StaffMember.objects.filter(household=household, user__is_active=True).select_related("user"))


class TaskForm(NoColon, forms.Form):
    title = forms.CharField(label="Task", max_length=160)
    notes = forms.CharField(label="Details", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    due_time = forms.TimeField(label="Time (optional)", required=False, widget=forms.TimeInput(attrs={"type": "time"}))

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"] = staff_choices_field(household)
        self.fields.update(when_fields())
        self.order_fields(["title", "assigned_to", "recurrence", "due_date", "weekdays", "due_time", "notes"])

    def clean(self):
        return clean_task_when(self, super().clean())


class QuickTaskForm(NoColon, forms.Form):
    preset = forms.IntegerField()

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"] = staff_choices_field(household)
        self.fields.update(when_fields())
        self.fields["due_date"].label = "Day"

    def clean(self):
        return clean_task_when(self, super().clean())


class HouseholdSettingsForm(NoColon, forms.ModelForm):
    class Meta:
        model = Household
        fields = ["name", "late_grace_minutes", "forgotten_checkout_hours", "notify_on_late", "notify_email"]
        labels = {"name": "Home name", "late_grace_minutes": "Late after (minutes)",
                  "forgotten_checkout_hours": "Flag missed check-out after (hours)",
                  "notify_on_late": "Email me about late check-ins", "notify_email": "Alert email"}
