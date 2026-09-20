from django.contrib import admin

from .models import (
    Batch,
    Chamber,
    ChamberEvent,
    PlannedAction,
    Protocol,
    SampleAssignment,
    SampleException,
    SampleUnit,
    SamplingPoint,
    StorageCondition,
    TestItem,
    TestResult,
)


@admin.register(Protocol)
class ProtocolAdmin(admin.ModelAdmin):
    list_display = ("code", "version", "title", "status", "effective_date", "supersedes")
    list_filter = ("status",)


admin.site.register(StorageCondition)
admin.site.register(SamplingPoint)
admin.site.register(TestItem)
admin.site.register(Chamber)


@admin.register(ChamberEvent)
class ChamberEventAdmin(admin.ModelAdmin):
    list_display = ("chamber", "event_type", "started_at", "ended_at", "status")
    list_filter = ("status", "event_type")


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("batch_number", "product_name", "protocol",
                    "storage_condition", "chamber", "storage_start",
                    "plan_generated")
    list_filter = ("protocol", "chamber")


@admin.register(SampleUnit)
class SampleUnitAdmin(admin.ModelAdmin):
    list_display = ("barcode", "batch", "status", "withdrawn_at")
    list_filter = ("status", "batch")


admin.site.register(PlannedAction)
admin.site.register(SampleAssignment)
admin.site.register(SampleException)
admin.site.register(TestResult)
