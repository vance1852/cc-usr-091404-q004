from django.contrib import admin

from .models import (
    Assay,
    AssayResult,
    Batch,
    Chamber,
    EnvironmentEvent,
    Exposure,
    ImpactAssessment,
    Product,
    Protocol,
    ProtocolVersion,
    ScheduledAction,
    SampleUnit,
    SamplingEvent,
    StorageCondition,
    TimePointDefinition,
    UserProfile,
)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role")
    list_filter = ("role",)


@admin.register(StorageCondition)
class StorageConditionAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "temp_low", "temp_high")


@admin.register(Chamber)
class ChamberAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "storage_condition", "is_active")
    list_filter = ("storage_condition", "is_active")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "dosage_form")


@admin.register(Assay)
class AssayAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "unit")


class TimePointInline(admin.TabularInline):
    model = TimePointDefinition
    extra = 0


@admin.register(Protocol)
class ProtocolAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "product")


@admin.register(ProtocolVersion)
class ProtocolVersionAdmin(admin.ModelAdmin):
    list_display = ("protocol", "version_no", "status", "storage_condition", "effective_date")
    list_filter = ("status",)
    inlines = [TimePointInline]


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("batch_no", "product", "protocol_version", "chamber", "manufacturing_date")


class ExposureInline(admin.TabularInline):
    model = Exposure
    extra = 0
    readonly_fields = ("event", "exposure_started_at", "exposure_ended_at", "minutes_exposed")


@admin.register(SampleUnit)
class SampleUnitAdmin(admin.ModelAdmin):
    list_display = ("barcode", "batch", "chamber", "status", "placed_at", "consumed_at")
    list_filter = ("status", "chamber")
    inlines = [ExposureInline]


@admin.register(EnvironmentEvent)
class EnvironmentEventAdmin(admin.ModelAdmin):
    list_display = ("chamber", "event_type", "started_at", "ended_at",
                    "min_temp", "max_temp", "duration_minutes")
    list_filter = ("event_type", "chamber")


@admin.register(ImpactAssessment)
class ImpactAssessmentAdmin(admin.ModelAdmin):
    list_display = ("exposure", "decision", "assessed_by", "assessed_at")


class ResultInline(admin.TabularInline):
    model = AssayResult
    extra = 0
    readonly_fields = ("recorded_by", "recorded_at", "is_frozen", "freeze_reason",
                       "disposition", "disposition_by", "disposition_at")


@admin.register(ScheduledAction)
class ScheduledActionAdmin(admin.ModelAdmin):
    list_display = ("batch", "timepoint_code", "kind", "status", "planned_at",
                    "sample", "freeze_reason")
    list_filter = ("status", "kind", "freeze_reason")
    inlines = [ResultInline]


@admin.register(SamplingEvent)
class SamplingEventAdmin(admin.ModelAdmin):
    list_display = ("action", "pulled_at", "pulled_by")
