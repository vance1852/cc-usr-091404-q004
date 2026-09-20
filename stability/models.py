"""
Domain model for the vaccine stability orchestration platform.

Key invariants enforced here and in :mod:`stability.services`:

* Plan time and actual time live in different columns.  ``SamplingPoint`` /
  ``PlannedAction`` hold *planned* datetimes; ``SampleUnit.withdrawn_at`` and
  ``TestResult.analyzed_at`` hold *actual* datetimes.  Actual sampling time is
  never copied into a planned field.
* A protocol version is immutable once a batch runs against it: batches point
  at the exact ``Protocol`` row and plan datetimes are snapshotted onto each
  ``PlannedAction``.  New protocol versions never rewrite old batches.
* Consumed samples are terminal — they cannot be assigned again.
* Substitution creates a *new* assignment; the original planned action is
  never mutated.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


# ---------------------------------------------------------------------------
# Protocol versioning
# ---------------------------------------------------------------------------

class Protocol(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草案"
        ACTIVE = "active", "现行"
        SUPERSEDED = "superseded", "被替代"
        RETIRED = "retired", "废止"

    code = models.CharField("方案编号", max_length=40)
    version = models.PositiveIntegerField("版本号", default=1)
    title = models.CharField("方案名称", max_length=200)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    effective_date = models.DateField("生效日期")
    supersedes = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="successors", verbose_name="替代的方案版本",
    )
    notes = models.TextField("说明", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="protocols_created", verbose_name="创建人",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("code", "version")
        ordering = ("code", "-version")

    def __str__(self):
        return f"{self.code} v{self.version}"


class StorageCondition(models.Model):
    protocol = models.ForeignKey(
        Protocol, on_delete=models.CASCADE,
        related_name="conditions", verbose_name="方案",
    )
    code = models.CharField("条件编号", max_length=20)  # e.g. 2-8C / 25C/60RH
    label = models.CharField("条件名称", max_length=120)
    target_temperature_c = models.DecimalField(
        "目标温度 ℃", max_digits=5, decimal_places=2, default=Decimal("5.0"),
    )
    target_humidity_pct = models.DecimalField(
        "目标相对湿度 %", max_digits=5, decimal_places=2, null=True, blank=True,
    )

    class Meta:
        unique_together = ("protocol", "code")
        ordering = ("protocol", "code")

    def __str__(self):
        return f"{self.protocol} / {self.code}"


class SamplingPoint(models.Model):
    """A planned time point, e.g. month 3, with its acceptance window."""

    protocol = models.ForeignKey(
        Protocol, on_delete=models.CASCADE,
        related_name="points", verbose_name="方案",
    )
    code = models.CharField("时间点编号", max_length=20)  # T0 / M1 / M3 ...
    name = models.CharField("时间点名称", max_length=80, blank=True)
    offset_days = models.PositiveIntegerField("距入库天数")
    window_before_days = models.PositiveIntegerField("窗口提前天数", default=0)
    window_after_days = models.PositiveIntegerField("窗口延后天数", default=0)
    order = models.PositiveIntegerField("顺序", default=0)
    test_items = models.ManyToManyField(
        "TestItem", related_name="points", verbose_name="该点检测项目",
    )

    class Meta:
        unique_together = ("protocol", "code")
        ordering = ("protocol", "order", "offset_days", "code")

    def __str__(self):
        return f"{self.protocol} / {self.code}"


class TestItem(models.Model):
    protocol = models.ForeignKey(
        Protocol, on_delete=models.CASCADE,
        related_name="test_items", verbose_name="方案",
    )
    code = models.CharField("项目编号", max_length=20)
    name = models.CharField("项目名称", max_length=120)
    method = models.CharField("检验方法", max_length=160, blank=True)
    unit = models.CharField("单位", max_length=30, blank=True)
    specification = models.CharField("质量标准", max_length=240, blank=True)

    class Meta:
        unique_together = ("protocol", "code")
        ordering = ("protocol", "code")

    def __str__(self):
        return f"{self.code} {self.name}"


# ---------------------------------------------------------------------------
# Chambers and environmental events
# ---------------------------------------------------------------------------

class Chamber(models.Model):
    code = models.CharField("恒温箱编号", max_length=30, unique=True)
    name = models.CharField("名称", max_length=120, blank=True)
    location = models.CharField("位置", max_length=120, blank=True)
    condition_note = models.CharField("设定条件", max_length=160, blank=True)

    class Meta:
        ordering = ("code",)

    def __str__(self):
        return self.code


class ChamberEvent(models.Model):
    """Power loss / temperature excursion etc.

    Until QA finishes the impact assessment (``status`` open / under review)
    every result from a sample physically present in the chamber during the
    event is *frozen* — QA cannot include it in the shelf-life evaluation.
    """

    class EventType(models.TextChoices):
        POWER_OUT = "power_out", "断电"
        TEMP_EXCURSION = "temp_excursion", "温度偏离"
        DOOR_OPEN = "door_open", "长时间开门"
        EQUIPMENT_FAILURE = "equipment_failure", "设备故障"
        OTHER = "other", "其他"

    class Assessment(models.TextChoices):
        OPEN = "open", "待评估"
        UNDER_REVIEW = "under_review", "评估中"
        NO_IMPACT = "no_impact", "评估完成-无影响"
        IMPACT = "impact", "评估完成-有影响"

    chamber = models.ForeignKey(
        Chamber, on_delete=models.CASCADE,
        related_name="events", verbose_name="恒温箱",
    )
    event_type = models.CharField("事件类型", max_length=24, choices=EventType.choices)
    started_at = models.DateTimeField("开始时间")
    ended_at = models.DateTimeField("结束时间", null=True, blank=True,
                                    help_text="为空表示事件仍在持续")
    observed_min_temp_c = models.DecimalField(
        "实测最低温度 ℃", max_digits=5, decimal_places=2, null=True, blank=True)
    observed_max_temp_c = models.DecimalField(
        "实测最高温度 ℃", max_digits=5, decimal_places=2, null=True, blank=True)
    description = models.TextField("描述", blank=True)
    status = models.CharField(
        "评估状态", max_length=20, choices=Assessment.choices, default=Assessment.OPEN)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="events_recorded", verbose_name="记录人",
    )
    recorded_at = models.DateTimeField(auto_now_add=True)
    assessed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="events_assessed", verbose_name="评估人",
    )
    assessed_at = models.DateTimeField("评估时间", null=True, blank=True)
    assessment_note = models.TextField("评估结论说明", blank=True)

    class Meta:
        ordering = ("-started_at",)

    @property
    def pending(self) -> bool:
        return self.status in (self.Assessment.OPEN, self.Assessment.UNDER_REVIEW)

    @property
    def assessed_impact(self) -> bool:
        return self.status == self.Assessment.IMPACT

    def __str__(self):
        return f"{self.chamber.code} {self.get_event_type_display()} {self.started_at:%Y-%m-%d %H:%M}"


# ---------------------------------------------------------------------------
# Batches and sample units
# ---------------------------------------------------------------------------

class Batch(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "考察中"
        COMPLETED = "completed", "已完成"
        TERMINATED = "terminated", "已终止"

    protocol = models.ForeignKey(
        Protocol, on_delete=models.PROTECT,
        related_name="batches", verbose_name="方案版本",
    )
    product_code = models.CharField("产品编号", max_length=40)
    product_name = models.CharField("产品名称", max_length=160, blank=True)
    batch_number = models.CharField("批号", max_length=40, unique=True)
    storage_condition = models.ForeignKey(
        StorageCondition, on_delete=models.PROTECT,
        related_name="batches", verbose_name="储存条件",
    )
    chamber = models.ForeignKey(
        Chamber, on_delete=models.PROTECT,
        related_name="batches", verbose_name="恒温箱",
    )
    storage_start = models.DateTimeField("入库时间（考察零点）")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    plan_generated = models.BooleanField("是否已编排计划", default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="batches_created", verbose_name="创建人",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("batch_number",)

    def __str__(self):
        return f"{self.batch_number} ({self.protocol})"


class SampleUnit(models.Model):
    """A physical sample unit.  Its lifecycle is a strict state machine."""

    class Status(models.TextChoices):
        IN_STORAGE = "in_storage", "在箱"
        RESERVED = "reserved", "已分配"
        WITHDRAWN = "withdrawn", "已取出"
        CONSUMED = "consumed", "已检验消耗"
        DAMAGED = "damaged", "破损"
        MISPLACED = "misplaced", "错放/遗失"

    TERMINAL_STATUSES = (Status.CONSUMED, Status.DAMAGED, Status.MISPLACED)

    batch = models.ForeignKey(
        Batch, on_delete=models.CASCADE,
        related_name="samples", verbose_name="批次",
    )
    barcode = models.CharField("样品条码", max_length=40, unique=True)
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.IN_STORAGE)
    # ACTUAL withdrawal time — never overwritten with, nor displayed as, the
    # planned sampling time.
    withdrawn_at = models.DateTimeField("实际取出时间", null=True, blank=True)
    withdrawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="samples_withdrawn", verbose_name="取出操作人",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("batch", "barcode")

    @property
    def terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES

    def __str__(self):
        return self.barcode


class SampleException(models.Model):
    """Misplacement / damage / early withdrawal etc. recorded against a unit.

    Pending dispositions freeze the unit's results until QA impact assessment.
    """

    class Kind(models.TextChoices):
        MISPLACED = "misplaced", "错放/找不到样品"
        DAMAGED = "damaged", "破损"
        WRONG_LOCATION = "wrong_location", "存放位置错误"
        EARLY_WITHDRAWAL = "early_withdrawal", "提前取出"
        LATE_WITHDRAWAL = "late_withdrawal", "逾期取出"
        OTHER = "other", "其他"

    class Disposition(models.TextChoices):
        PENDING = "pending", "待影响评估"
        NO_IMPACT = "no_impact", "无影响"
        IMPACT = "impact", "有影响"

    sample = models.ForeignKey(
        SampleUnit, on_delete=models.CASCADE,
        related_name="exceptions", verbose_name="样品",
    )
    assignment = models.ForeignKey(
        "SampleAssignment", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="exceptions", verbose_name="关联分配",
    )
    kind = models.CharField("异常类型", max_length=20, choices=Kind.choices)
    occurred_at = models.DateTimeField("发生时间")
    note = models.TextField("说明", blank=True)
    disposition = models.CharField(
        "影响评估", max_length=16, choices=Disposition.choices,
        default=Disposition.PENDING,
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="exceptions_recorded", verbose_name="记录人",
    )
    recorded_at = models.DateTimeField(auto_now_add=True)
    assessed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="exceptions_assessed", verbose_name="评估人",
    )
    assessed_at = models.DateTimeField("评估时间", null=True, blank=True)
    assessment_note = models.TextField("评估说明", blank=True)

    class Meta:
        ordering = ("-occurred_at",)

    @property
    def pending(self) -> bool:
        return self.disposition == self.Disposition.PENDING

    @property
    def assessed_impact(self) -> bool:
        return self.disposition == self.Disposition.IMPACT

    def __str__(self):
        return f"{self.sample.barcode} {self.get_kind_display()}"


# ---------------------------------------------------------------------------
# Planned actions and assignments
# ---------------------------------------------------------------------------

class PlannedAction(models.Model):
    """One (batch, time point, test item) action with SNAPSHOTTED plan times.

    Generated from the protocol version pinned by the batch; later protocol
    versions never touch these rows.
    """

    batch = models.ForeignKey(
        Batch, on_delete=models.CASCADE,
        related_name="actions", verbose_name="批次",
    )
    sampling_point = models.ForeignKey(
        SamplingPoint, on_delete=models.PROTECT,
        related_name="actions", verbose_name="计划时间点",
    )
    test_item = models.ForeignKey(
        TestItem, on_delete=models.PROTECT,
        related_name="actions", verbose_name="检测项目",
    )
    point_code = models.CharField("时间点编号（快照）", max_length=20)
    point_name = models.CharField("时间点名称（快照）", max_length=80, blank=True)
    planned_datetime = models.DateTimeField("计划取样时间")
    window_start = models.DateTimeField("窗口开始")
    window_end = models.DateTimeField("窗口结束")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("batch", "sampling_point", "test_item")
        ordering = ("planned_datetime", "test_item__code")

    @property
    def original_assignment(self):
        return self.assignments.filter(role=SampleAssignment.Role.ORIGINAL).first()

    @property
    def active_assignment(self):
        """The assignment a result could currently be submitted against."""
        return self.assignments.filter(state=SampleAssignment.State.ACTIVE).order_by(
            "-created_at").first()

    def __str__(self):
        return f"{self.batch.batch_number} {self.point_code}/{self.test_item.code}"


class SampleAssignment(models.Model):
    """Designation of a physical sample unit to fulfill a planned action.

    Substitutes get their own row and point at the assignment they replace;
    the ``PlannedAction`` itself is never modified.
    """

    class Role(models.TextChoices):
        ORIGINAL = "original", "原计划样品"
        SUBSTITUTE = "substitute", "替代样品"

    class State(models.TextChoices):
        ACTIVE = "active", "有效"
        FAILED = "failed", "已失败（样品不可用）"

    class Reason(models.TextChoices):
        DAMAGED = "damaged", "破损"
        MISPLACED = "misplaced", "错放/遗失"
        LOST = "lost", "灭失"
        EARLY_WITHDRAWAL = "early_withdrawal", "提前取出"
        INSUFFICIENT = "insufficient", "样品量不足"
        OTHER = "other", "其他"

    action = models.ForeignKey(
        PlannedAction, on_delete=models.CASCADE,
        related_name="assignments", verbose_name="计划动作",
    )
    sample = models.ForeignKey(
        SampleUnit, on_delete=models.PROTECT,
        related_name="assignments", verbose_name="样品",
    )
    role = models.CharField(max_length=12, choices=Role.choices)
    state = models.CharField(
        max_length=12, choices=State.choices, default=State.ACTIVE)
    substitutes = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="replaced_by", verbose_name="替代的分配",
    )
    reason = models.CharField(
        "替代原因", max_length=20, choices=Reason.choices, blank=True)
    note = models.TextField("说明", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="assignments_created", verbose_name="安排人",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)

    @property
    def is_substitute(self) -> bool:
        return self.role == self.Role.SUBSTITUTE

    def __str__(self):
        return f"{self.action_id}:{self.sample.barcode}({self.get_role_display()})"


# ---------------------------------------------------------------------------
# Test results and QA dispositions
# ---------------------------------------------------------------------------

class TestResult(models.Model):
    class QAStatus(models.TextChoices):
        PENDING = "pending", "待质量判定"
        INCLUDED = "included", "纳入"
        EXCLUDED = "excluded", "排除"
        ADDITIONAL = "additional", "追加考察"

    assignment = models.ForeignKey(
        SampleAssignment, on_delete=models.PROTECT,
        related_name="results", verbose_name="执行分配",
    )
    test_item = models.ForeignKey(
        TestItem, on_delete=models.PROTECT,
        related_name="results", verbose_name="检测项目",
    )
    value = models.DecimalField(
        "数值结果", max_digits=16, decimal_places=6, null=True, blank=True)
    value_text = models.CharField("文字结果", max_length=240, blank=True)
    unit = models.CharField("单位", max_length=30, blank=True)
    # ACTUAL test execution time (distinct from planned time).
    analyzed_at = models.DateTimeField("实际检测时间")
    analyst = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="results_submitted", verbose_name="分析员",
    )
    submitted_at = models.DateTimeField("提交时间", default=timezone.now)
    qa_status = models.CharField(
        "质量判定", max_length=12, choices=QAStatus.choices, default=QAStatus.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="results_decided", verbose_name="判定人",
    )
    decided_at = models.DateTimeField("判定时间", null=True, blank=True)
    decision_note = models.TextField("判定说明", blank=True)

    class Meta:
        unique_together = ("assignment", "test_item")
        ordering = ("-analyzed_at",)

    def __str__(self):
        return f"{self.assignment.sample.barcode} {self.test_item.code}"

    @property
    def action(self) -> PlannedAction:
        return self.assignment.action

    @property
    def sample(self) -> SampleUnit:
        return self.assignment.sample
