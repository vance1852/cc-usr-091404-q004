"""领域服务：计划生成、样品分配、实际取样、环境事件冻结、影响评估、替代安排。

关键不变量：
- ScheduledAction 的计划字段（planned_at/window_*）生成后永不回写；
- 实际取样时间只保存在 SamplingEvent.pulled_at；
- 样品一旦消耗（consumed）不能再分配；
- 温度偏离/破损/错放先冻结动作与结果，评估后才放行或排除；
- 替代动作是新行，原计划行保持不变。
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import (
    ActionKind,
    ActionStatus,
    AssayResult,
    Batch,
    Disposition,
    EnvironmentEvent,
    EventType,
    Exposure,
    FreezeReason,
    ImpactAssessment,
    ImpactDecision,
    ScheduledAction,
    SampleStatus,
    SampleUnit,
    SamplingEvent,
)


class ServiceError(Exception):
    """业务规则冲突（HTTP 层映射为 400）。"""


# ================================================================ 计划生成
@transaction.atomic
def schedule_actions_for_batch(batch: Batch, *, created_by=None) -> list[ScheduledAction]:
    """按批次入组的方案版本生成全部计划取样动作。"""
    pv = batch.protocol_version
    zero = timezone.make_aware(
        timezone.datetime.combine(batch.manufacturing_date, timezone.datetime.min.time())
    )
    existing = {a.timepoint_id for a in batch.actions.filter(kind=ActionKind.SCHEDULED)}
    created = []
    for tp in pv.timepoints.all():
        if tp.id in existing:
            continue
        planned = zero + timedelta(days=tp.offset_days)
        action = ScheduledAction.objects.create(
            batch=batch,
            protocol_version=pv,
            timepoint=tp,
            timepoint_code=tp.code,
            offset_days=tp.offset_days,
            kind=ActionKind.SCHEDULED,
            status=ActionStatus.PENDING,
            planned_at=planned,
            window_start=planned - timedelta(hours=tp.window_before_hours),
            window_end=planned + timedelta(hours=tp.window_after_hours),
            created_by=created_by,
        )
        created.append(action)
    return created


# ================================================================ 样品分配
@transaction.atomic
def assign_sample(action: ScheduledAction, sample: SampleUnit, *, user=None) -> ScheduledAction:
    if action.kind == ActionKind.SCHEDULED and action.status not in (
        ActionStatus.PENDING,
        ActionStatus.FROZEN,
    ):
        raise ServiceError("该动作已取样或作废，不能分配样品。")
    if action.status == ActionStatus.VOID:
        raise ServiceError("已作废动作不能分配样品。")
    if sample.batch_id != action.batch_id:
        raise ServiceError("样品与动作不属于同一批次。")
    if sample.status != SampleStatus.AVAILABLE:
        raise ServiceError(f"样品当前状态为“{sample.get_status_display()}”，不可分配。")
    if sample.has_open_exposure:
        raise ServiceError("样品存在未完成影响评估的温度偏离，暂不能分配。")
    # 同一样品不能同时挂在另一个未终结动作上
    busy = sample.actions.filter(
        status__in=(ActionStatus.PENDING, ActionStatus.FROZEN)
    ).exclude(pk=action.pk).exists()
    if busy:
        raise ServiceError("样品已分配给另一个未完成动作。")

    action.sample = sample
    action.assigned_at = timezone.now()
    action.save(update_fields=["sample", "assigned_at"])
    return action


# ================================================================ 实际取样
@transaction.atomic
def record_sampling(action: ScheduledAction, pulled_at, user, note="") -> SamplingEvent:
    """登记实际取样。提前/逾窗只做标记，绝不改写计划时间。"""
    if action.status == ActionStatus.VOID:
        raise ServiceError("已作废动作不能取样。")
    if action.status == ActionStatus.PULLED:
        raise ServiceError("动作已有实际取样记录。")
    if action.status == ActionStatus.FROZEN:
        raise ServiceError("动作处于冻结状态，须等待 QA 影响评估后再取样。")
    sample = action.sample
    if sample is None:
        raise ServiceError("动作尚未分配样品。")
    if sample.status != SampleStatus.AVAILABLE:
        raise ServiceError(f"样品状态为“{sample.get_status_display()}”，不能取样。")

    event = SamplingEvent.objects.create(
        action=action, pulled_at=pulled_at, pulled_by=user, note=note
    )
    action.status = ActionStatus.PULLED
    action.save(update_fields=["status"])
    # 已消耗样品锁定，不可再分配
    sample.status = SampleStatus.CONSUMED
    sample.consumed_at = pulled_at
    sample.save(update_fields=["status", "consumed_at"])
    return event


# ================================================================ 样品异常
@transaction.atomic
def mark_sample_problem(sample: SampleUnit, status: str, note: str) -> SampleUnit:
    """错放/破损：冻结样品关联的未终结动作与检测结果。"""
    if status not in (SampleStatus.MISPLACED, SampleStatus.DAMAGED):
        raise ServiceError("只允许标记为错放或破损。")
    # 已消耗样品在实验室发现破损时，样品状态保持 consumed 不可再分配，
    # 但关联结果必须冻结等待 QA 影响评估。
    freeze_related = True
    if sample.status == SampleStatus.CONSUMED:
        if status == SampleStatus.MISPLACED:
            raise ServiceError("已消耗样品不再适用错放标记。")
    elif sample.status != SampleStatus.AVAILABLE:
        raise ServiceError(f"样品当前状态为“{sample.get_status_display()}”，不能标记异常。")
    else:
        sample.status = status
    sample.note = note
    sample.save(update_fields=["status", "note"])

    reason = (
        FreezeReason.MISPLACED if status == SampleStatus.MISPLACED else FreezeReason.DAMAGED
    )
    _freeze_sample(sample, reason, note)
    return sample


@transaction.atomic
def mark_sample_found(sample: SampleUnit, note="") -> SampleUnit:
    """错放样品找回：无未决暴露时解除冻结。"""
    if sample.status != SampleStatus.MISPLACED:
        raise ServiceError("只有错放状态的样品可以找回。")
    sample.status = SampleStatus.AVAILABLE
    if note:
        sample.note = note
    sample.save(update_fields=["status", "note"])
    _try_unfreeze(sample)
    return sample


# ================================================================ 环境事件
@transaction.atomic
def log_environment_event(
    chamber, event_type, started_at, ended_at=None, *,
    min_temp=None, max_temp=None, description="", recorded_by=None,
) -> EnvironmentEvent:
    if ended_at and ended_at <= started_at:
        raise ServiceError("事件结束时间必须晚于开始时间。")
    event = EnvironmentEvent.objects.create(
        chamber=chamber,
        event_type=event_type,
        started_at=started_at,
        ended_at=ended_at,
        min_temp=min_temp,
        max_temp=max_temp,
        description=description,
        recorded_by=recorded_by,
    )
    if not event.is_temperature_deviation:
        return event

    # 找到事件期间在箱（或在期间被取走）的样品，计算暴露并冻结
    samples = SampleUnit.objects.filter(
        chamber=chamber,
        placed_at__lte=event.ended_at or timezone.now(),
    ).exclude(status__in=(SampleStatus.WITHDRAWN, SampleStatus.DAMAGED))

    for sample in samples:
        presence_end = sample.consumed_at  # 消耗即离开箱体
        overlap_start = max(sample.placed_at, event.started_at)
        overlap_end = min(
            presence_end or (event.ended_at or timezone.now()),
            event.ended_at or timezone.now(),
        )
        if overlap_end <= overlap_start:
            continue
        minutes = int((overlap_end - overlap_start).total_seconds() // 60)
        if minutes <= 0:
            continue
        exposure = Exposure.objects.create(
            sample=sample,
            event=event,
            exposure_started_at=overlap_start,
            exposure_ended_at=overlap_end,
            minutes_exposed=minutes,
        )
        _freeze_sample(sample, FreezeReason.TEMP_EXCURSION,
                       f"事件 {event} 暴露 {minutes} 分钟")
    return event


# ================================================================ 影响评估
@transaction.atomic
def assess_impact(exposure: Exposure, decision: str, user, comment="") -> ImpactAssessment:
    if ImpactAssessment.objects.filter(exposure=exposure).exists():
        raise ServiceError("该暴露已有影响评估结论。")
    if decision not in dict(ImpactDecision.CHOICES):
        raise ServiceError("无效的评估结论。")
    assessment = ImpactAssessment.objects.create(
        exposure=exposure, decision=decision, comment=comment, assessed_by=user
    )
    sample = exposure.sample

    if decision == ImpactDecision.EXCLUDE:
        # 排除受影响单元：结果判排除，未取样动作作废，可用样品退出
        AssayResult.objects.filter(
            action__sample=sample, is_frozen=True
        ).update(
            disposition=Disposition.EXCLUDED,
            disposition_by=user,
            disposition_at=timezone.now(),
            disposition_comment=f"温度偏离影响评估排除：{comment}",
        )
        sample.actions.filter(status=ActionStatus.FROZEN).update(
            status=ActionStatus.VOID,
            freeze_note="温度偏离影响评估排除，动作作废",
        )
        if sample.status == SampleStatus.AVAILABLE:
            sample.status = SampleStatus.WITHDRAWN
            sample.save(update_fields=["status"])
        _try_unfreeze(sample)
    else:
        # release / additional：暴露已闭环，无其他未决因素即解冻；
        # additional 的追加考察由协调员另行建立 extra 动作，结果仍待 QA 逐项处置。
        _try_unfreeze(sample)
    return assessment


# ================================================================ 替代取样
@transaction.atomic
def create_replacement(
    original: ScheduledAction, sample: SampleUnit, user, *,
    planned_at=None, note=""
) -> ScheduledAction:
    """为无法执行的原计划动作安排替代样品。原计划行保持不变。"""
    if original.kind != ActionKind.SCHEDULED:
        raise ServiceError("只能为原计划动作安排替代。")
    if original.status not in (ActionStatus.FROZEN, ActionStatus.VOID,
                               ActionStatus.PENDING, ActionStatus.PULLED):
        raise ServiceError("原动作状态不支持安排替代。")
    if original.status == ActionStatus.PENDING and not original.is_overdue:
        raise ServiceError("原动作仍在取样窗口内，应直接取样而非安排替代。")
    if original.status == ActionStatus.PULLED:
        # 已正常取样的动作，只有全部结果被 QA 排除（且无未决/冻结项）才可替代
        results = list(original.results.all())
        if not results:
            raise ServiceError("原动作尚无检测结果，不能以替代方式重取。")
        if any(r.is_frozen for r in results):
            raise ServiceError("原动作结果仍在冻结评估中，暂不能安排替代。")
        if any(r.disposition != Disposition.EXCLUDED for r in results):
            raise ServiceError("仅当原动作结果全部被 QA 排除时，才能安排替代样品。")
    if sample.batch_id != original.batch_id:
        raise ServiceError("替代样品必须来自同一批次。")
    if sample.status != SampleStatus.AVAILABLE or sample.has_open_exposure:
        raise ServiceError("替代样品须为在箱可用且无未决偏离的样品。")

    planned = planned_at or timezone.now()
    window_hours = original.protocol_version.default_pull_window_hours
    replacement = ScheduledAction.objects.create(
        batch=original.batch,
        protocol_version=original.protocol_version,
        timepoint=original.timepoint,
        timepoint_code=original.timepoint_code,
        offset_days=original.offset_days,
        kind=ActionKind.REPLACEMENT,
        status=ActionStatus.PENDING,
        planned_at=planned,
        window_start=planned - timedelta(hours=window_hours // 2),
        window_end=planned + timedelta(hours=window_hours - window_hours // 2),
        replaces=original,
        created_by=user,
        freeze_note=note,
    )
    assign_sample(replacement, sample, user=user)
    return replacement


# ================================================================ 追加考察
@transaction.atomic
def create_extra_action(batch, timepoint_code, offset_days, sample, user, *,
                        planned_at=None, note="") -> ScheduledAction:
    """QA 决定追加考察后，协调员为批次新增额外动作（不改原方案计划）。"""
    if sample.batch_id != batch.id:
        raise ServiceError("样品与批次不一致。")
    if sample.status != SampleStatus.AVAILABLE or sample.has_open_exposure:
        raise ServiceError("样品不可用。")
    planned = planned_at or timezone.now()
    action = ScheduledAction.objects.create(
        batch=batch,
        protocol_version=batch.protocol_version,
        timepoint=None,
        timepoint_code=timepoint_code,
        offset_days=offset_days,
        kind=ActionKind.EXTRA,
        planned_at=planned,
        window_start=planned - timedelta(hours=24),
        window_end=planned + timedelta(hours=72),
        created_by=user,
        freeze_note=note,
    )
    assign_sample(action, sample, user=user)
    return action


# ================================================================ 检测结果
@transaction.atomic
def submit_result(action: ScheduledAction, assay, value: str, user) -> AssayResult:
    """分析员仅提交结果；冻结动作不能提交，处置由 QA 完成。"""
    if action.status != ActionStatus.PULLED:
        raise ServiceError("只能为已取样动作提交检测结果。")
    # 替代/追加动作只检测其方案时间点定义中的项目
    if action.timepoint_id and not action.timepoint.assays.filter(pk=assay.pk).exists():
        raise ServiceError("该检测项目不在此时间点的方案检测范围内。")
    if AssayResult.objects.filter(action=action, assay=assay).exists():
        raise ServiceError("该检测项目已有结果，如需更正请联系 QA。")
    return AssayResult.objects.create(
        action=action, assay=assay, value=value, recorded_by=user
    )


@transaction.atomic
def dispose_result(result: AssayResult, disposition: str, user, comment="") -> AssayResult:
    """QA 决定纳入 / 排除 / 追加考察。"""
    if disposition not in (Disposition.INCLUDED, Disposition.EXCLUDED, Disposition.ADDITIONAL):
        raise ServiceError("无效的处置结论。")
    if result.is_frozen:
        raise ServiceError("结果仍处于冻结状态，须先完成影响评估。")
    result.disposition = disposition
    result.disposition_by = user
    result.disposition_at = timezone.now()
    result.disposition_comment = comment
    result.save(update_fields=[
        "disposition", "disposition_by", "disposition_at", "disposition_comment"
    ])
    return result


# ================================================================ 内部冻结原语
def _freeze_sample(sample: SampleUnit, reason: str, note: str):
    now = timezone.now()
    for action in sample.actions.exclude(status=ActionStatus.VOID).exclude(
        status=ActionStatus.PULLED
    ):
        if action.status != ActionStatus.FROZEN:
            action.status = ActionStatus.FROZEN
            action.frozen_at = now
        action.freeze_reason = reason
        action.freeze_note = note
        action.save(update_fields=["status", "freeze_reason", "frozen_at", "freeze_note"])

    # 已取样动作：动作本身标记 pulled，但结果必须冻结
    results = AssayResult.objects.filter(action__sample=sample)
    if reason == FreezeReason.TEMP_EXCURSION:
        results = results.exclude(disposition__in=[Disposition.INCLUDED, Disposition.EXCLUDED])
    for result in results:
        if not result.is_frozen:
            result.is_frozen = True
            result.frozen_at = now
            result.freeze_reason = reason
            result.save(update_fields=["is_frozen", "frozen_at", "freeze_reason"])


def _try_unfreeze(sample: SampleUnit):
    """所有冻结原因消除后（无未决暴露 + 样品恢复可用）才解冻。"""
    if sample.status not in (SampleStatus.AVAILABLE, SampleStatus.CONSUMED):
        return
    if sample.has_open_exposure:
        return
    for action in sample.actions.filter(status=ActionStatus.FROZEN):
        # 冻结期间禁止取样，解冻动作必回到待取样
        action.status = ActionStatus.PENDING
        action.freeze_reason = None
        action.frozen_at = None
        action.freeze_note = ""
        action.save(update_fields=["status", "freeze_reason", "frozen_at", "freeze_note"])

    AssayResult.objects.filter(action__sample=sample, is_frozen=True).update(
        is_frozen=False, freeze_reason=None, frozen_at=None
    )
