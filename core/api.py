"""REST API：按角色隔离的编排接口。

角色分工：
- 协调员 coordinator：维护主数据/方案/批次/样品、生成计划、分配样品、登记取样、
  记录环境事件、安排替代与追加考察；
- 分析员 analyst：仅提交检测结果；
- 质量人员 qa：记录/评估环境事件影响、对结果做纳入/排除/追加考察处置。
所有登录用户均可只读查询时间轴、逾期、暴露与趋势数据集。
"""
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action as drf_action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .models import (
    Assay,
    AssayResult,
    Batch,
    Chamber,
    Disposition,
    EnvironmentEvent,
    Exposure,
    Product,
    Protocol,
    ProtocolVersion,
    ScheduledAction,
    SampleUnit,
    StorageCondition,
    TimePointDefinition,
)
from .permissions import IsAnalyst, IsCoordinator, IsCoordinatorOrQA, IsQA
from .serializers import (
    ActionListSerializer,
    AssayResultSerializer,
    AssaySerializer,
    BatchSerializer,
    ChamberSerializer,
    DispositionInputSerializer,
    EnvironmentEventSerializer,
    EventInputSerializer,
    ExposureSerializer,
    ExtraActionInputSerializer,
    ProductSerializer,
    ProtocolSerializer,
    ProtocolVersionSerializer,
    ReplacementInputSerializer,
    ResultInputSerializer,
    SampleProblemSerializer,
    SampleUnitSerializer,
    SamplingInputSerializer,
    StorageConditionSerializer,
    TimePointDefinitionSerializer,
    AssignSerializer,
    AssessmentInputSerializer,
)


def _role(user):
    return getattr(user.profile, "role", "") if hasattr(user, "profile") else ""


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        u = request.user
        return Response({
            "username": u.username,
            "is_superuser": u.is_superuser,
            "role": _role(u),
        })


# ---------------------------------------------------------------- 主数据（协调员可写）
class _CoordinatorReadOnlyModelViewSet(viewsets.ModelViewSet):
    def get_permissions(self):
        if self.action in ("create", "update", "partial_update", "destroy"):
            return [IsCoordinator()]
        # 自定义 @action 通过路由以实例属性注入 permission_classes，须一并尊重
        return [p() for p in self.permission_classes]


class StorageConditionViewSet(_CoordinatorReadOnlyModelViewSet):
    queryset = StorageCondition.objects.all()
    serializer_class = StorageConditionSerializer


class ChamberViewSet(_CoordinatorReadOnlyModelViewSet):
    queryset = Chamber.objects.select_related("storage_condition").all()
    serializer_class = ChamberSerializer


class ProductViewSet(_CoordinatorReadOnlyModelViewSet):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer


class AssayViewSet(_CoordinatorReadOnlyModelViewSet):
    queryset = Assay.objects.all()
    serializer_class = AssaySerializer


# ---------------------------------------------------------------- 方案版本
class ProtocolViewSet(_CoordinatorReadOnlyModelViewSet):
    queryset = Protocol.objects.select_related("product").all()
    serializer_class = ProtocolSerializer


class ProtocolVersionViewSet(_CoordinatorReadOnlyModelViewSet):
    queryset = ProtocolVersion.objects.select_related(
        "protocol", "storage_condition"
    ).prefetch_related("timepoints__assays")
    serializer_class = ProtocolVersionSerializer


class TimePointViewSet(viewsets.ModelViewSet):
    """方案时间点定义。仅草案版本可增改；生效后计划冻结。"""

    queryset = TimePointDefinition.objects.prefetch_related("assays")
    serializer_class = TimePointDefinitionSerializer

    def get_permissions(self):
        if self.action in ("create", "update", "partial_update", "destroy"):
            return [IsCoordinator()]
        return [IsAuthenticated()]

    def _check_editable(self, pv):
        if pv.status != ProtocolVersion.Status.DRAFT:
            return Response(
                {"detail": f"方案版本为“{pv.get_status_display()}”，时间点不可修改。"},
                status=status.HTTP_409_CONFLICT,
            )
        return None

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        conflict = self._check_editable(serializer.validated_data["protocol_version"])
        if conflict:
            return conflict
        assays = serializer.validated_data.pop("assays", [])
        tp = TimePointDefinition.objects.create(**serializer.validated_data)
        tp.assays.set(assays)
        return Response(self.get_serializer(tp).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        conflict = self._check_editable(instance.protocol_version)
        if conflict:
            return conflict
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        assays = serializer.validated_data.pop("assays", None)
        for k, v in serializer.validated_data.items():
            setattr(instance, k, v)
        instance.save()
        if assays is not None:
            instance.assays.set(assays)
        return Response(self.get_serializer(instance).data)


# ---------------------------------------------------------------- 批次
class BatchViewSet(_CoordinatorReadOnlyModelViewSet):
    queryset = Batch.objects.select_related(
        "product", "protocol_version", "protocol_version__protocol", "chamber"
    )
    serializer_class = BatchSerializer

    def perform_create(self, serializer):
        batch = serializer.save(created_by=self.request.user)
        services.schedule_actions_for_batch(batch, created_by=self.request.user)

    @drf_action(detail=True, methods=["post"], permission_classes=[IsCoordinator])
    def regenerate_schedule(self, request, pk=None):
        """方案时间点补充后为批次补齐缺失的计划动作（不改动已生成行）。"""
        batch = self.get_object()
        created = services.schedule_actions_for_batch(batch, created_by=request.user)
        return Response(
            {"created": [a.id for a in created], "count": len(created)},
            status=status.HTTP_201_CREATED,
        )

    @drf_action(detail=True, methods=["get"])
    def timeline(self, request, pk=None):
        """批次完整时间轴：计划/实际/替代/冻结/结果。"""
        batch = self.get_object()
        actions = (
            batch.actions.select_related("sample", "sampling_event", "timepoint")
            .prefetch_related("results__assay", "replacements__sample")
            .order_by("planned_at", "id")
        )
        exposures = Exposure.objects.filter(
            sample__batch=batch
        ).select_related("event", "impact_assessment")
        return Response({
            "batch": BatchSerializer(batch).data,
            "actions": ActionListSerializer(actions, many=True).data,
            "exposures": ExposureSerializer(exposures, many=True).data,
        })


# ---------------------------------------------------------------- 样品
class SampleViewSet(viewsets.ModelViewSet):
    queryset = SampleUnit.objects.select_related("batch", "chamber")
    serializer_class = SampleUnitSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_permissions(self):
        if self.action == "create":
            return [IsCoordinator()]
        return [p() for p in self.permission_classes]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        if params.get("batch"):
            qs = qs.filter(batch_id=params["batch"])
        if params.get("chamber"):
            qs = qs.filter(chamber_id=params["chamber"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("available") == "true":
            qs = qs.filter(status="available")
        return qs

    @drf_action(detail=True, methods=["post"], permission_classes=[IsCoordinator])
    def problem(self, request, pk=None):
        """标记错放/破损 -> 自动冻结相关动作与结果。"""
        sample = self.get_object()
        ser = SampleProblemSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            services.mark_sample_problem(sample, ser.validated_data["status"],
                                         ser.validated_data.get("note", ""))
        except services.ServiceError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(sample).data)

    @drf_action(detail=True, methods=["post"], permission_classes=[IsCoordinator],
                url_path="found")
    def mark_found(self, request, pk=None):
        sample = self.get_object()
        try:
            services.mark_sample_found(sample, request.data.get("note", ""))
        except services.ServiceError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(sample).data)


# ---------------------------------------------------------------- 动作
class ActionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ActionListSerializer

    def get_queryset(self):
        qs = (
            ScheduledAction.objects.select_related(
                "sample", "sampling_event", "batch", "timepoint", "replaces"
            )
            .prefetch_related("results__assay", "replacements__sample")
        )
        p = self.request.query_params
        if p.get("batch"):
            qs = qs.filter(batch_id=p["batch"])
        if p.get("status"):
            qs = qs.filter(status=p["status"])
        if p.get("kind"):
            qs = qs.filter(kind=p["kind"])
        if p.get("overdue") == "true":
            qs = [a for a in qs if a.is_overdue]
        return qs

    @drf_action(detail=True, methods=["post"], permission_classes=[IsCoordinator])
    def assign(self, request, pk=None):
        action = self.get_object()
        ser = AssignSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        sample = get_object_or_404(SampleUnit, pk=ser.validated_data["sample_id"])
        try:
            services.assign_sample(action, sample, user=request.user)
        except services.ServiceError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(action).data)

    @drf_action(detail=True, methods=["post"], permission_classes=[IsCoordinator])
    def sample(self, request, pk=None):
        """登记实际取样时间（独立于计划时间保存）。"""
        action = self.get_object()
        ser = SamplingInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            services.record_sampling(
                action, ser.validated_data["pulled_at"], request.user,
                ser.validated_data.get("note", ""),
            )
        except services.ServiceError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(action).data)

    @drf_action(detail=True, methods=["post"], permission_classes=[IsCoordinator])
    def replace(self, request, pk=None):
        """为缺失时间点安排替代样品（原计划行不变）。"""
        original = self.get_object()
        ser = ReplacementInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        sample = get_object_or_404(SampleUnit, pk=ser.validated_data["sample_id"])
        try:
            repl = services.create_replacement(
                original, sample, request.user,
                planned_at=ser.validated_data.get("planned_at"),
                note=ser.validated_data.get("note", ""),
            )
        except services.ServiceError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(repl).data,
                        status=status.HTTP_201_CREATED)

    @drf_action(detail=True, methods=["post"],
                permission_classes=[IsCoordinatorOrQA], url_path="extra")
    def extra(self, request, pk=None):
        """在批次上安排追加考察动作（QA 提出、协调员执行，二者皆可调）。"""
        batch = get_object_or_404(Batch, pk=pk)
        ser = ExtraActionInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        sample = get_object_or_404(SampleUnit, pk=ser.validated_data["sample_id"])
        try:
            act = services.create_extra_action(
                batch,
                ser.validated_data["timepoint_code"],
                ser.validated_data["offset_days"],
                sample, request.user,
                planned_at=ser.validated_data.get("planned_at"),
                note=ser.validated_data.get("note", ""),
            )
        except services.ServiceError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(act).data, status=status.HTTP_201_CREATED)


class OverdueView(APIView):
    """全部逾期动作（窗口已过仍待取样）。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (
            ScheduledAction.objects.filter(status="pending")
            .select_related("batch", "sample")
        )
        overdue = [a for a in qs if a.is_overdue]
        overdue.sort(key=lambda a: a.window_end)
        return Response(ActionListSerializer(overdue, many=True).data)


# ---------------------------------------------------------------- 环境事件 / 暴露
class EnvironmentEventViewSet(viewsets.ModelViewSet):
    queryset = EnvironmentEvent.objects.select_related("chamber", "recorded_by")
    serializer_class = EnvironmentEventSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_permissions(self):
        # 协调员与 QA 均可登记箱体事件
        if self.action in ("create", "update", "partial_update", "destroy"):
            return [IsCoordinatorOrQA()]
        return [IsAuthenticated()]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("chamber"):
            qs = qs.filter(chamber_id=self.request.query_params["chamber"])
        return qs

    def perform_create(self, serializer):
        serializer.save(recorded_by=self.request.user)

    def create(self, request, *args, **kwargs):
        ser = EventInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            event = services.log_environment_event(
                recorded_by=request.user, **ser.validated_data
            )
        except services.ServiceError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(EnvironmentEventSerializer(event).data,
                        status=status.HTTP_201_CREATED)


class ExposureViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Exposure.objects.select_related(
        "sample", "event", "event__chamber", "impact_assessment"
    )
    serializer_class = ExposureSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        p = self.request.query_params
        if p.get("open") == "true":
            qs = qs.filter(impact_assessment__isnull=True)
        if p.get("chamber"):
            qs = qs.filter(event__chamber_id=p["chamber"])
        if p.get("sample"):
            qs = qs.filter(sample_id=p["sample"])
        return qs

    @drf_action(detail=True, methods=["post"], permission_classes=[IsQA],
                url_path="assess")
    def assess(self, request, pk=None):
        """QA 影响评估：放行解冻 / 排除 / 追加考察。"""
        exposure = self.get_object()
        ser = AssessmentInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            services.assess_impact(
                exposure, ser.validated_data["decision"], request.user,
                ser.validated_data.get("comment", ""),
            )
        except services.ServiceError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        exposure.refresh_from_db()
        return Response(self.get_serializer(exposure).data)


# ---------------------------------------------------------------- 检测结果
class AssayResultViewSet(viewsets.ModelViewSet):
    queryset = AssayResult.objects.select_related(
        "action", "assay", "recorded_by", "disposition_by"
    )
    serializer_class = AssayResultSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_permissions(self):
        # 分析员仅负责提交结果；处置走独立的 QA dispose 动作
        if self.action == "create":
            return [IsAnalyst()]
        # dispose 等 @action 的角色由路由注入的 permission_classes 决定
        return [p() for p in self.permission_classes]

    def get_queryset(self):
        qs = super().get_queryset()
        p = self.request.query_params
        if p.get("batch"):
            qs = qs.filter(action__batch_id=p["batch"])
        if p.get("assay"):
            qs = qs.filter(assay_id=p["assay"])
        if p.get("disposition"):
            qs = qs.filter(disposition=p["disposition"])
        if p.get("frozen") == "true":
            qs = qs.filter(is_frozen=True)
        if p.get("usable") == "true":
            qs = [r for r in qs if r.usable_for_trend]
        return qs

    def create(self, request, *args, **kwargs):
        ser = ResultInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        from .models import Assay
        action = get_object_or_404(ScheduledAction, pk=request.data.get("action_id"))
        assay = get_object_or_404(Assay, pk=ser.validated_data["assay_id"])
        try:
            result = services.submit_result(
                action, assay, ser.validated_data["value"], request.user
            )
        except services.ServiceError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(result).data,
                        status=status.HTTP_201_CREATED)

    @drf_action(detail=True, methods=["post"], permission_classes=[IsQA])
    def dispose(self, request, pk=None):
        result = self.get_object()
        ser = DispositionInputSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            services.dispose_result(
                result, ser.validated_data["disposition"], request.user,
                ser.validated_data.get("comment", ""),
            )
        except services.ServiceError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(result).data)


# ---------------------------------------------------------------- 趋势数据集
class TrendDatasetView(APIView):
    """当前可用于货架期趋势分析的数据集：仅 QA 已纳入且未冻结的结果，
    并同时返回计划与实际取样时间（二者不做替换）。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (
            AssayResult.objects.filter(
                disposition=Disposition.INCLUDED, is_frozen=False
            )
            .select_related(
                "action", "action__batch", "action__batch__product",
                "assay", "action__sampling_event",
            )
            .order_by("action__batch_id", "assay_id", "action__planned_at")
        )
        if request.query_params.get("batch"):
            qs = qs.filter(action__batch_id=request.query_params["batch"])
        if request.query_params.get("product"):
            qs = qs.filter(action__batch__product_id=request.query_params["product"])

        rows = []
        for r in qs:
            se = getattr(r.action, "sampling_event", None)
            rows.append({
                "batch_no": r.action.batch.batch_no,
                "product_code": r.action.batch.product.code,
                "assay_code": r.assay.code,
                "assay_name": r.assay.name,
                "unit": r.assay.unit,
                "timepoint_code": r.action.timepoint_code,
                "kind": r.action.kind,
                "offset_days": r.action.offset_days,
                "planned_at": r.action.planned_at,
                "pulled_at": se.pulled_at if se else None,
                "pull_offset_hours": se.offset_from_planned_hours if se else None,
                "early": se.early if se else None,
                "late": se.late if se else None,
                "value": r.value,
                "replaces_action_id": r.action.replaces_id,
            })
        return Response({"count": len(rows), "results": rows})
