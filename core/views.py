"""Sign-in, first-run setup, invitations, and the home page router."""
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.views import LoginView
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import views_owner, views_staff
from .forms import LoginForm, SetupForm, StaffSignupForm
from .models import Household, Invitation, StaffMember
from .permissions import get_role
from .services import log

User = get_user_model()


def home(request):
    if not Household.objects.exists():
        return redirect("setup")
    role = get_role(request.user)
    if role == "owner":
        return views_owner.dashboard(request)
    if role == "staff":
        return views_staff.staff_home(request)
    if request.user.is_authenticated:
        logout(request)
        messages.error(request, "That account isn't linked to a home.")
    return redirect("login")


def login_view(request):
    if not Household.objects.exists():
        return redirect("setup")
    return LoginView.as_view(template_name="core/login.html", authentication_form=LoginForm,
                             redirect_authenticated_user=True)(request)


def setup(request):
    """First run only: create the home and the owner account."""
    if Household.objects.exists():
        return redirect("home")
    form = SetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            names = data["your_name"].strip().split(" ", 1)
            user = User.objects.create_user(
                username=data["email"], email=data["email"], password=data["password1"],
                first_name=names[0], last_name=names[1] if len(names) > 1 else "",
                is_staff=True, is_superuser=True)
            household = Household.objects.create(name=data["household_name"], owner=user)
            log(household, user, "Home created", household.name)
        login(request, user)
        messages.success(request, "Your home is set up. Next, invite your nannies.")
        return redirect("staff_list")
    return render(request, "core/setup.html", {"form": form})


def accept_invite(request, token):
    invitation = get_object_or_404(Invitation, token=token)
    if not invitation.is_usable:
        return render(request, "core/invite_invalid.html", {"invitation": invitation}, status=410)
    if not request.on_home_network:
        return render(request, "core/network_blocked.html", status=403)
    if request.user.is_authenticated:
        logout(request)

    form = StaffSignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if User.objects.filter(username=invitation.email).exists():
            form.add_error(None, "An account already exists for this email. Ask to have it switched back on.")
        else:
            data = form.cleaned_data
            with transaction.atomic():
                user = User.objects.create_user(
                    username=invitation.email, email=invitation.email, password=data["password1"],
                    first_name=data["first_name"].strip(), last_name=data["last_name"].strip())
                staff = StaffMember.objects.create(
                    household=invitation.household, user=user, hourly_rate=invitation.hourly_rate,
                    interac=data["interac"])
                invitation.accepted_at = timezone.now()
                invitation.save(update_fields=["accepted_at"])
                log(invitation.household, user, "Invite accepted", f"{staff.name} ({user.email}) joined.")
            login(request, user)
            messages.success(request, f"Welcome, {staff.first_name}.")
            return redirect("home")
    return render(request, "core/invite_accept.html", {"form": form, "invitation": invitation})
