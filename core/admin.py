from django.contrib import admin

from . import models

for model in (models.Household, models.StaffMember, models.ScheduleSlot, models.TimeEntry, models.TimeEditRequest,
              models.TaskPreset, models.Task, models.TaskCompletion, models.Invitation, models.PayRecord):
    admin.site.register(model)


@admin.register(models.AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "details")
    readonly_fields = ("household", "actor", "action", "details", "created_at")
