"""领域模型：方案版本、储存条件、样品、取样动作、环境事件、检测结果与 QA 处置。"""
from django.conf import settings
from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------- 常量 / 枚举
class Role:
    COORDINATOR = "coordinator"
    ANALYST = "analyst"
    QA = "qa"
    CHOICES = [
        (COORDINATOR, "协调员"),
        (ANALYST, "分析员"),
        (QA, "质量人员"),
    ]


class SampleStatus:
    AVAILABLE = "available"
    CONSUMED = "consumed"
    DAMAGED = "damaged"
    MISPLACED = "misplaced"
    WITHDRAWN = "withdrawn"
    CHOICES = [
        (AVAILABLE, "在箱可用"),
        (CONSUMED, "已消耗"),
        (DAMAGED, "破损"),
        (MISPLACED, "错放/失联"),
        (WITHDRAWN, "已退出"),
    ]


class ActionKind:
    SCHEDULED = "scheduled"
    REPLACEMENT = "replacement"
    EXTRA = "extra"  # QA 追加考察
    CHOICES = [
        (SCHEDULED, "计划取样"),
        (REPLACEMENT, "替代取样"),
        (EXTRA, "追加考察"),
    ]


class ActionStatus:
    PENDING = "pending"
    PULLED = "pulled"
    FROZEN = "frozen"
    VOID = "void"
    CHOICES = [
        (PENDING, "待取样"),
        (PULLED, "已取样"),
        (FROZEN, "冻结待评估"),
        (VOID, "已作废"),
    ]


class FreezeReason:
    TEMP_EXCURSION = "temp_excursion"
    DAMAGED = "damaged"
    MISPLACED = "misplaced"
    CHOICES = [
        (TEMP_EXCURSION, "温度偏离"),
        (DAMAGED, "样品破损"),
        (MISPLACED, "样品错放"),
    ]


class Disposition:
    PENDING = "pending_review"      # 等待 QA 判定（含冻结解除后）
    INCLUDED = "included"          # 纳入货架期评估
    EXCLUDED = "excluded"          # 排除
    ADDITIONAL = "additional"      # 追加考察
    CHOICES = [
        (PENDING, "待判定"),
        (INCLUDED, "纳入"),
        (EXCLUDED, "排除"),
        (ADDITIONAL, "追加考察"),
    ]


class EventType:
    POWER_OUTAGE = "power_outage"
    TEMP_EXCURSION = "temp_excursion"
    DOOR_OPEN = "door_open"
    OTHER = "other"
    CHOICES = [
        (POWER_OUTAGE, "断电"),
        (TEMP_EXCURSION, "温度偏离"),
        (DOOR_OPEN, "长时间开门"),
        (OTHER, "其他"),
    ]


class ImpactDecision:
    RELEASE = "release"
    EXCLUDE = "exclude"
    ADDITIONAL = "additional"
    CHOICES = [
        (RELEASE, "放行解冻"),
        (EXCLUDE, "排除影响单元"),
        (ADDITIONAL, "追加考察"),
    ]


# ---------------------------------------------------------------- 基础主数据
class UserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile"
    )
    role = models.CharField("角色", max_length=16, choices=Role.CHOICES)

    class Meta:
        verbose_name = "用户角色"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"


class StorageCondition(models.Model):
    code = models.CharField("储存条件编码", max_length=32, unique=True)
    name = models.CharField("名称", max_length=64)
    temp_low = models.DecimalField("温度下限 ℃", max_digits=5, decimal_places=1)
    temp_high = models.DecimalField("温度上限 ℃", max_digits=5, decimal_places=1)
    humidity_low = models.DecimalField("湿度下限 %RH", max_digits=5, decimal_places=1, null=True, blank=True)
    humidity_high = models.DecimalField("湿度上限 %RH", max_digits=5, decimal_places=1, null=True, blank=True)

    class Meta:
        verbose_name = "储存条件"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.code} {self.name}"


class Chamber(models.Model):
    code = models.CharField("恒温箱编号", max_length=32, unique=True)
    name = models.CharField("名称", max_length=64)
    storage_condition = models.ForeignKey(
        StorageCondition, verbose_name="规定储存条件", on_delete=models.PROTECT
    )
    location = models.CharField("位置", max_length=128, blank=True)
    is_active = models.BooleanField("启用中", default=True)

    class Meta:
        verbose_name = "恒温箱"
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.code


class Product(models.Model):
    code = models.CharField("产品编码", max_length=32, unique=True)
    name = models.CharField("产品名称", max_length=128)
    dosage_form = models.CharField("剂型", max_length=32, blank=True)

    class Meta:
        verbose_name = "产品"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.code} {self.name}"


class Assay(models.Model):
    code = models.CharField("检测项目编码", max_length=32, unique=True)
    name = models.CharField("检测项目", max_length=128)
    unit = models.CharField("单位", max_length=32, blank=True)
    method = models.CharField("方法", max_length=128, blank=True)
    lower_spec = models.DecimalField("质量标准下限", max_digits=12, decimal_places=4, null=True, blank=True)
    upper_spec = models.DecimalField("质量标准上限", max_digits=12, decimal_places=4, null=True, blank=True)

    class Meta:
        verbose_name = "检测项目"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.code} {self.name}"


# ---------------------------------------------------------------- 方案与版本
class Protocol(models.Model):
    product = models.ForeignKey(Product, verbose_name="产品", on_delete=models.PROTECT, related_name="protocols")
    code = models.CharField("方案编号", max_length=32, unique=True)
    title = models.CharField("方案标题", max_length=200)

    class Meta:
        verbose_name = "稳定性方案"
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.code


class ProtocolVersion(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草案"
        EFFECTIVE = "effective", "生效"
        SUPERSEDED = "superseded", "被替代"

    protocol = models.ForeignKey(Protocol, verbose_name="方案", on_delete=models.PROTECT, related_name="versions")
    version_no = models.CharField("版本号", max_length=16)
    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.DRAFT)
    storage_condition = models.ForeignKey(
        StorageCondition, verbose_name="储存条件", on_delete=models.PROTECT
    )
    default_pull_window_hours = models.PositiveIntegerField("默认取样窗口(小时)", default=48)
    effective_date = models.DateField("生效日期", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "方案版本"
        verbose_name_plural = verbose_name
        unique_together = ("protocol", "version_no")
        ordering = ["protocol__code", "-created_at"]

    def __str__(self):
        return f"{self.protocol.code} v{self.version_no}"


class TimePointDefinition(models.Model):
    """方案版本中的计划时间点（偏移天数 + 取样窗口 + 检测项目）。"""

    protocol_version = models.ForeignKey(
        ProtocolVersion, verbose_name="方案版本", on_delete=models.CASCADE, related_name="timepoints"
    )
    code = models.CharField("时间点标识", max_length=16)  # 0M / 3M / 6M ...
    offset_days = models.PositiveIntegerField("距零点天数")
    window_before_hours = models.PositiveIntegerField("窗口提前小时数", default=48)
    window_after_hours = models.PositiveIntegerField("窗口延后小时数", default=48)
    assays = models.ManyToManyField(Assay, verbose_name="检测项目", related_name="timepoints")

    class Meta:
        verbose_name = "计划时间点定义"
        verbose_name_plural = verbose_name
        unique_together = ("protocol_version", "code")
        ordering = ["protocol_version", "offset_days"]

    def __str__(self):
        return f"{self.protocol_version} {self.code}"


# ---------------------------------------------------------------- 批次与样品
class Batch(models.Model):
    product = models.ForeignKey(Product, verbose_name="产品", on_delete=models.PROTECT, related_name="batches")
    batch_no = models.CharField("批号", max_length=32, unique=True)
    protocol_version = models.ForeignKey(
        ProtocolVersion, verbose_name="入组方案版本", on_delete=models.PROTECT, related_name="batches"
    )
    chamber = models.ForeignKey(Chamber, verbose_name="放置恒温箱", on_delete=models.PROTECT, related_name="batches")
    manufacturing_date = models.DateField("生产日期(稳定性零点)")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "批次"
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.batch_no


class SampleUnit(models.Model):
    barcode = models.CharField("样品条码", max_length=64, unique=True)
    batch = models.ForeignKey(Batch, verbose_name="批号", on_delete=models.PROTECT, related_name="samples")
    chamber = models.ForeignKey(
        Chamber, verbose_name="所在恒温箱", on_delete=models.PROTECT, related_name="samples"
    )
    status = models.CharField("状态", max_length=16, choices=SampleStatus.CHOICES, default=SampleStatus.AVAILABLE)
    placed_at = models.DateTimeField("入箱时间", default=timezone.now)
    consumed_at = models.DateTimeField("消耗时间", null=True, blank=True)
    note = models.CharField("备注", max_length=255, blank=True)

    class Meta:
        verbose_name = "样品单元"
        verbose_name_plural = verbose_name
        ordering = ["batch__batch_no", "barcode"]

    def __str__(self):
        return self.barcode

    @property
    def has_open_exposure(self) -> bool:
        return self.exposures.filter(impact_assessment__isnull=True).exists()


# ---------------------------------------------------------------- 环境事件
class EnvironmentEvent(models.Model):
    chamber = models.ForeignKey(Chamber, verbose_name="恒温箱", on_delete=models.PROTECT, related_name="events")
    event_type = models.CharField("事件类型", max_length=16, choices=EventType.CHOICES)
    started_at = models.DateTimeField("开始时间")
    ended_at = models.DateTimeField("结束时间", null=True, blank=True)
    min_temp = models.DecimalField("期间最低温度 ℃", max_digits=5, decimal_places=1, null=True, blank=True)
    max_temp = models.DecimalField("期间最高温度 ℃", max_digits=5, decimal_places=1, null=True, blank=True)
    description = models.CharField("描述", max_length=255, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="logged_events", null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "箱体环境事件"
        verbose_name_plural = verbose_name
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.chamber.code} {self.get_event_type_display()} {self.started_at:%Y-%m-%d %H:%M}"

    @property
    def duration_minutes(self) -> int:
        end = self.ended_at or timezone.now()
        return max(0, int((end - self.started_at).total_seconds() // 60))

    @property
    def is_temperature_deviation(self) -> bool:
        """温度是否超出该箱规定储存条件。"""
        cond = self.chamber.storage_condition
        if self.max_temp is not None and self.max_temp > cond.temp_high:
            return True
        if self.min_temp is not None and self.min_temp < cond.temp_low:
            return True
        # 断电/长时间开门即使没有温度读数也按潜在偏离处理
        return self.event_type in (EventType.POWER_OUTAGE, EventType.DOOR_OPEN)


class Exposure(models.Model):
    """样品对某次环境事件的暴露记录，冻结/影响评估的工作对象。"""

    sample = models.ForeignKey(
        SampleUnit, verbose_name="暴露样品", on_delete=models.PROTECT, related_name="exposures"
    )
    event = models.ForeignKey(
        EnvironmentEvent, verbose_name="环境事件", on_delete=models.PROTECT, related_name="exposures"
    )
    exposure_started_at = models.DateTimeField("暴露开始")
    exposure_ended_at = models.DateTimeField("暴露结束")
    minutes_exposed = models.PositiveIntegerField("暴露时长(分钟)")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "偏离暴露"
        verbose_name_plural = verbose_name
        unique_together = ("sample", "event")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.sample.barcode} 暴露 {self.minutes_exposed} 分钟"


class ImpactAssessment(models.Model):
    exposure = models.OneToOneField(
        Exposure, verbose_name="暴露记录", on_delete=models.PROTECT, related_name="impact_assessment"
    )
    decision = models.CharField("评估结论", max_length=16, choices=ImpactDecision.CHOICES)
    comment = models.TextField("评估说明", blank=True)
    assessed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="QA 评估人", on_delete=models.PROTECT, related_name="assessments"
    )
    assessed_at = models.DateTimeField("评估时间", auto_now=True)

    class Meta:
        verbose_name = "影响评估"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.exposure} -> {self.get_decision_display()}"


# ---------------------------------------------------------------- 取样动作
class ScheduledAction(models.Model):
    """按方案版本为批次生成的可执行动作。计划字段一经生成不可修改。"""

    batch = models.ForeignKey(Batch, verbose_name="批次", on_delete=models.PROTECT, related_name="actions")
    protocol_version = models.ForeignKey(
        ProtocolVersion, verbose_name="方案版本快像", on_delete=models.PROTECT, related_name="actions"
    )
    timepoint = models.ForeignKey(
        TimePointDefinition, verbose_name="计划时间点", on_delete=models.PROTECT,
        related_name="actions", null=True, blank=True,
    )
    timepoint_code = models.CharField("时间点标识(快像)", max_length=16)
    offset_days = models.PositiveIntegerField("偏移天数(快像)")

    kind = models.CharField("动作类型", max_length=16, choices=ActionKind.CHOICES, default=ActionKind.SCHEDULED)
    status = models.CharField("状态", max_length=16, choices=ActionStatus.CHOICES, default=ActionStatus.PENDING)

    # ---- 计划字段（不可变；实际取样时间在 SamplingEvent 中单独记录）----
    planned_at = models.DateTimeField("计划取样时间")
    window_start = models.DateTimeField("窗口开始")
    window_end = models.DateTimeField("窗口结束")

    # ---- 样品分配 ----
    sample = models.ForeignKey(
        SampleUnit, verbose_name="分配样品", on_delete=models.PROTECT,
        related_name="actions", null=True, blank=True,
    )
    assigned_at = models.DateTimeField("分配时间", null=True, blank=True)

    # ---- 替代关系（替代动作不改原计划行）----
    replaces = models.ForeignKey(
        "self", verbose_name="替代的原动作", on_delete=models.PROTECT,
        related_name="replacements", null=True, blank=True,
    )

    # ---- 冻结 ----
    freeze_reason = models.CharField("冻结原因", max_length=32, choices=FreezeReason.CHOICES, null=True, blank=True)
    frozen_at = models.DateTimeField("冻结时间", null=True, blank=True)
    freeze_note = models.CharField("冻结说明", max_length=255, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_actions", null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "取样动作"
        verbose_name_plural = verbose_name
        ordering = ["batch__batch_no", "planned_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "timepoint"],
                condition=models.Q(kind="scheduled"),
                name="uniq_scheduled_action_per_batch_timepoint",
            ),
        ]

    def __str__(self):
        return f"{self.batch.batch_no} {self.timepoint_code} [{self.get_kind_display()}]"

    # ---- 时间轴派生属性 ----
    @property
    def is_overdue(self) -> bool:
        return (
            self.status == ActionStatus.PENDING
            and timezone.now() > self.window_end
        )

    @property
    def is_window_open(self) -> bool:
        if self.status != ActionStatus.PENDING:
            return False
        now = timezone.now()
        return self.window_start <= now <= self.window_end


class SamplingEvent(models.Model):
    """实际取样记录。实际时间独立保存，绝不回写计划时间。"""

    action = models.OneToOneField(
        ScheduledAction, verbose_name="动作", on_delete=models.PROTECT, related_name="sampling_event"
    )
    pulled_at = models.DateTimeField("实际取样时间")
    pulled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="取样人", on_delete=models.PROTECT, related_name="samplings"
    )
    note = models.CharField("备注", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "实际取样记录"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.action} @ {self.pulled_at:%Y-%m-%d %H:%M}"

    @property
    def early(self) -> bool:
        return self.pulled_at < self.action.window_start

    @property
    def late(self) -> bool:
        return self.pulled_at > self.action.window_end

    @property
    def offset_from_planned_hours(self) -> float:
        return round((self.pulled_at - self.action.planned_at).total_seconds() / 3600, 1)


# ---------------------------------------------------------------- 检测结果
class AssayResult(models.Model):
    action = models.ForeignKey(
        ScheduledAction, verbose_name="取样动作", on_delete=models.PROTECT, related_name="results"
    )
    assay = models.ForeignKey(Assay, verbose_name="检测项目", on_delete=models.PROTECT, related_name="results")
    value = models.CharField("结果值", max_length=64)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="分析员", on_delete=models.PROTECT, related_name="recorded_results"
    )
    recorded_at = models.DateTimeField("检测时间", default=timezone.now)

    # ---- 冻结 ----
    is_frozen = models.BooleanField("冻结待评估", default=False)
    freeze_reason = models.CharField("冻结原因", max_length=32, choices=FreezeReason.CHOICES, null=True, blank=True)
    frozen_at = models.DateTimeField("冻结时间", null=True, blank=True)

    # ---- QA 处置（分析员无权修改）----
    disposition = models.CharField(
        "QA 处置", max_length=16, choices=Disposition.CHOICES, default=Disposition.PENDING
    )
    disposition_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="QA 处置人",
        on_delete=models.PROTECT, related_name="disposed_results", null=True, blank=True,
    )
    disposition_at = models.DateTimeField("处置时间", null=True, blank=True)
    disposition_comment = models.CharField("处置说明", max_length=255, blank=True)

    class Meta:
        verbose_name = "检测结果"
        verbose_name_plural = verbose_name
        unique_together = ("action", "assay")
        ordering = ["action__batch__batch_no", "action__planned_at", "assay__code"]

    def __str__(self):
        return f"{self.action} {self.assay.code}={self.value}"

    @property
    def usable_for_trend(self) -> bool:
        """当前可用于货架期趋势分析。"""
        return self.disposition == Disposition.INCLUDED and not self.is_frozen
