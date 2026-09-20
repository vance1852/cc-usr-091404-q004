from rest_framework import serializers

from .models import (
    Assay,
    AssayResult,
    Batch,
    Chamber,
    Disposition,
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
)


# ---------------------------------------------------------------- 主数据
class StorageConditionSerializer(serializers.ModelSerializer):
    class Meta:
        model = StorageCondition
        fields = "__all__"


class ChamberSerializer(serializers.ModelSerializer):
    storage_condition_code = serializers.CharField(source="storage_condition.code", read_only=True)

    class Meta:
        model = Chamber
        fields = "__all__"


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = "__all__"


class AssaySerializer(serializers.ModelSerializer):
    class Meta:
        model = Assay
        fields = "__all__"


# ---------------------------------------------------------------- 方案
class ProtocolSerializer(serializers.ModelSerializer):
    class Meta:
        model = Protocol
        fields = "__all__"


class TimePointDefinitionSerializer(serializers.ModelSerializer):
    assay_codes = serializers.SlugRelatedField(
        many=True, read_only=True, slug_field="code", source="assays"
    )

    class Meta:
        model = TimePointDefinition
        fields = ["id", "protocol_version", "code", "offset_days",
                  "window_before_hours", "window_after_hours", "assays", "assay_codes"]
        extra_kwargs = {
            "assays": {"write_only": True, "required": False, "allow_empty": True},
        }


class ProtocolVersionSerializer(serializers.ModelSerializer):
    timepoints = TimePointDefinitionSerializer(many=True, read_only=True)
    protocol_code = serializers.CharField(source="protocol.code", read_only=True)
    storage_condition_code = serializers.CharField(source="storage_condition.code", read_only=True)

    class Meta:
        model = ProtocolVersion
        fields = "__all__"


# ---------------------------------------------------------------- 批次 / 样品
class BatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Batch
        fields = "__all__"
        read_only_fields = ("created_by", "created_at")


class SampleUnitSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    has_open_exposure = serializers.BooleanField(read_only=True)
    chamber_code = serializers.CharField(source="chamber.code", read_only=True)
    batch_no = serializers.CharField(source="batch.batch_no", read_only=True)

    class Meta:
        model = SampleUnit
        fields = "__all__"
        read_only_fields = ("status", "consumed_at", "placed_at")


# ---------------------------------------------------------------- 事件 / 暴露
class EnvironmentEventSerializer(serializers.ModelSerializer):
    duration_minutes = serializers.IntegerField(read_only=True)
    is_temperature_deviation = serializers.BooleanField(read_only=True)
    event_type_display = serializers.CharField(source="get_event_type_display", read_only=True)

    class Meta:
        model = EnvironmentEvent
        fields = "__all__"
        read_only_fields = ("recorded_by", "created_at")


class ImpactAssessmentSerializer(serializers.ModelSerializer):
    decision_display = serializers.CharField(source="get_decision_display", read_only=True)
    assessed_by_username = serializers.CharField(source="assessed_by.username", read_only=True)

    class Meta:
        model = ImpactAssessment
        fields = "__all__"
        read_only_fields = ("assessed_by", "assessed_at")


class ExposureSerializer(serializers.ModelSerializer):
    event_type = serializers.CharField(source="event.get_event_type_display", read_only=True)
    event_id = serializers.IntegerField(source="event.id", read_only=True)
    chamber_code = serializers.CharField(source="event.chamber.code", read_only=True)
    sample_barcode = serializers.CharField(source="sample.barcode", read_only=True)
    batch_no = serializers.CharField(source="sample.batch.batch_no", read_only=True)
    impact_assessment = ImpactAssessmentSerializer(read_only=True)

    class Meta:
        model = Exposure
        fields = "__all__"


# ---------------------------------------------------------------- 动作 / 取样 / 结果
class SamplingEventSerializer(serializers.ModelSerializer):
    pulled_by_username = serializers.CharField(source="pulled_by.username", read_only=True)
    early = serializers.BooleanField(read_only=True)
    late = serializers.BooleanField(read_only=True)
    offset_from_planned_hours = serializers.FloatField(read_only=True)

    class Meta:
        model = SamplingEvent
        fields = "__all__"
        read_only_fields = ("pulled_by",)


class AssayResultSerializer(serializers.ModelSerializer):
    assay_code = serializers.CharField(source="assay.code", read_only=True)
    assay_name = serializers.CharField(source="assay.name", read_only=True)
    recorded_by_username = serializers.CharField(source="recorded_by.username", read_only=True)
    disposition_display = serializers.CharField(source="get_disposition_display", read_only=True)
    usable_for_trend = serializers.BooleanField(read_only=True)
    batch_no = serializers.CharField(source="action.batch.batch_no", read_only=True)
    timepoint_code = serializers.CharField(source="action.timepoint_code", read_only=True)
    kind = serializers.CharField(source="action.kind", read_only=True)
    pulled_at = serializers.DateTimeField(source="action.sampling_event.pulled_at", read_only=True)

    class Meta:
        model = AssayResult
        fields = "__all__"
        read_only_fields = (
            "is_frozen", "freeze_reason", "frozen_at",
            "disposition", "disposition_by", "disposition_at", "disposition_comment",
            "recorded_by", "recorded_at",
        )


class ActionListSerializer(serializers.ModelSerializer):
    """时间轴用：含计划、实际取样、样品、替代链、冻结与逾期信息。"""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    freeze_reason_display = serializers.CharField(source="get_freeze_reason_display", read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    is_window_open = serializers.BooleanField(read_only=True)
    sample_barcode = serializers.CharField(source="sample.barcode", default=None)
    sample_status = serializers.CharField(source="sample.status", default=None)
    sampling_event = SamplingEventSerializer(read_only=True)
    replacements = serializers.SerializerMethodField()
    replaces_id = serializers.IntegerField(read_only=True)
    results = AssayResultSerializer(many=True, read_only=True)

    class Meta:
        model = ScheduledAction
        fields = "__all__"

    def get_replacements(self, obj):
        return [
            {"id": r.id, "sample_barcode": r.sample.barcode if r.sample else None,
             "status": r.status, "planned_at": r.planned_at}
            for r in obj.replacements.all()
        ]


# ---------------------------------------------------------------- 操作输入
class AssignSerializer(serializers.Serializer):
    sample_id = serializers.IntegerField()


class SamplingInputSerializer(serializers.Serializer):
    pulled_at = serializers.DateTimeField()
    note = serializers.CharField(required=False, allow_blank=True, default="")


class SampleProblemSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["misplaced", "damaged"])
    note = serializers.CharField(required=False, allow_blank=True, default="")


class EventInputSerializer(serializers.ModelSerializer):
    class Meta:
        model = EnvironmentEvent
        fields = ["chamber", "event_type", "started_at", "ended_at",
                  "min_temp", "max_temp", "description"]


class AssessmentInputSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["release", "exclude", "additional"])
    comment = serializers.CharField(required=False, allow_blank=True, default="")


class ReplacementInputSerializer(serializers.Serializer):
    sample_id = serializers.IntegerField()
    planned_at = serializers.DateTimeField(required=False)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class ExtraActionInputSerializer(serializers.Serializer):
    timepoint_code = serializers.CharField(max_length=16)
    offset_days = serializers.IntegerField(min_value=0)
    sample_id = serializers.IntegerField()
    planned_at = serializers.DateTimeField(required=False)
    note = serializers.CharField(required=False, allow_blank=True, default="")


class ResultInputSerializer(serializers.Serializer):
    assay_id = serializers.IntegerField()
    value = serializers.CharField(max_length=64)


class DispositionInputSerializer(serializers.Serializer):
    disposition = serializers.ChoiceField(
        choices=[Disposition.INCLUDED, Disposition.EXCLUDED, Disposition.ADDITIONAL]
    )
    comment = serializers.CharField(required=False, allow_blank=True, default="")
