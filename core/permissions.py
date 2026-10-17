from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def get_role(user):
    """Return 'owner', 'staff' or None for the given user."""
    if not user or not user.is_authenticated:
        return None
    if hasattr(user, "owned_household"):
        return "owner"
    if hasattr(user, "staff_profile"):
        return "staff"
    return None


def owner_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if get_role(request.user) != "owner":
            raise PermissionDenied
        request.household = request.user.owned_household
        return view(request, *args, **kwargs)
    return wrapper


def staff_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if get_role(request.user) != "staff":
            raise PermissionDenied
        request.staff = request.user.staff_profile
        request.household = request.staff.household
        return view(request, *args, **kwargs)
    return wrapper
