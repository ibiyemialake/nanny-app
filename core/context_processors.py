from .permissions import get_role


def app_context(request):
    user = getattr(request, "user", None)
    role = get_role(user)
    household = None
    if role == "owner":
        household = user.owned_household
    elif role == "staff":
        household = user.staff_profile.household
    return {"role": role, "current_household": household}
