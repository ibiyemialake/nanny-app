from django.contrib.auth.views import LogoutView
from django.urls import path

from . import views, views_owner as owner, views_staff as staff

urlpatterns = [
    path("", views.home, name="home"),
    path("setup/", views.setup, name="setup"),
    path("login/", views.login_view, name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("invite/<uuid:token>/", views.accept_invite, name="accept_invite"),

    # Nannies
    path("me/check-in/", staff.check_in, name="check_in"),
    path("me/check-out/", staff.check_out, name="check_out"),
    path("me/hours/", staff.my_hours, name="my_hours"),
    path("me/shifts/<int:entry_id>/fix/", staff.request_correction, name="request_correction"),
    path("me/tasks/done/", staff.complete_task, name="complete_task"),
    path("me/tasks/undo/", staff.undo_task, name="undo_task"),

    # Owner
    path("staff/", owner.staff_list, name="staff_list"),
    path("staff/<int:pk>/", owner.staff_detail, name="staff_detail"),
    path("staff/<int:pk>/access/", owner.toggle_active, name="toggle_active"),
    path("staff/<int:pk>/paid/", owner.mark_paid, name="mark_paid"),
    path("payments/<int:pk>/undo/", owner.unmark_paid, name="unmark_paid"),
    path("invites/<int:pk>/cancel/", owner.revoke_invite, name="revoke_invite"),
    path("schedule/<int:pk>/delete/", owner.delete_slot, name="delete_slot"),
    path("hours/", owner.timesheet, name="timesheet"),
    path("hours/add/", owner.add_entry, name="add_entry"),
    path("hours/<int:pk>/", owner.edit_entry, name="edit_entry"),
    path("hours/fixes/<int:pk>/", owner.review_request, name="review_request"),
    path("hours/<int:pk>/late-checkout/", owner.review_late_checkout, name="review_late_checkout"),
    path("hours/<int:pk>/unscheduled/", owner.review_unscheduled, name="review_unscheduled"),
    path("tasks/", owner.tasks, name="tasks"),
    path("tasks/quick/", owner.quick_task, name="quick_task"),
    path("tasks/<int:pk>/remove/", owner.archive_task, name="archive_task"),
    path("tasks/buttons/add/", owner.add_preset, name="add_preset"),
    path("tasks/buttons/<int:pk>/delete/", owner.delete_preset, name="delete_preset"),
    path("activity/", owner.activity, name="activity"),
    path("settings/", owner.household_settings, name="household_settings"),
]
