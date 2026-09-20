"""
Service layer: every state transition in the platform goes through here.

Rules enforced:

* Planned times are snapshotted from the protocol version at plan generation
  and never rewritten.
* Only an in-storage, non-terminal, never-consumed sample can be assigned;
  consumed samples cannot be re-allocated to any action.
* Substitution appends a new assignment and fails the previous one; the
  original ``PlannedAction`` row is untouched.
* Withdrawal always records the *actual* time; withdrawals outside the
  planned window open a pending deviation that freezes related results.
* Misplacement / damage fail the active assignment and freeze results.
* Analysts only create results; include/exclude/additional is QA's decision,
  and inclusion is refused while an impact assessment is pending (frozen).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from .engine import _aware
from .exceptions import (
    FrozenError,
    SampleUnavailableError,
    ValidationError,
)
from .models import (
    Batch,
    ChamberEvent,
    PlannedAction,
    Protocol,
    SampleAssignment,
    SampleException,
    SampleUnit,
    SamplingPoint,
    TestResult,
)


# ---------------------------------------------------------------------------
# Protocol / plan generation
# ---------------------------------------------------------------------------

@transaction.atomic
def activate_protocol(protocol: Protocol) -> Protocol:
    if protocol.status == Protocol.Status.SUPERSEDED:
        raise ValidationError("已被替代的方案版本不能重新激活")
    if protocol.supersedes_id:
        Protocol.objects.filter(pk=protocol.supersedes_id).update(
            status=Protocol.Status.SUPERSEDED)
    protocol.status = Protocol.Status.ACTIVE
    protocol.save(update_fields=["status"])
    return protocol


@transaction.atomic
def generate_plan(batch: Batch) -> list[PlannedAction]:
    """Snapshot (point × test item) planned datetimes for the batch.

    Idempotent: regenerating never overwrites existing planned actions, so a
    later protocol edit/version cannot silently change an executing batch.
    """
    if batch.plan_generated or batch.actions.exists():
        raise ValidationError("该批次的计划已经生成，方案版本快照不可修改")

    protocol = batch.protocol
    zero = _aware(batch.storage_start)
    created: list[PlannedAction] = []
    points = (
        protocol.points.filter(order__gte=0)
        .prefetch_related("test_items")
    )
    for point in points:
        planned = zero + timedelta(days=point.offset_days)
        for item in point.test_items.all():
            created.append(PlannedAction(
                batch=batch,
                sampling_point=point,
                test_item=item,
                point_code=point.code,
                point_name=point.name,
                planned_datetime=planned,
                window_start=planned - timedelta(days=point.window_before_days),
                window_end=planned + timedelta(days=point.window_after_days),
            ))
    PlannedAction.objects.bulk_create(created)
    batch.plan_generated = True
    batch.save(update_fields=["plan_generated"])
    return created


# ---------------------------------------------------------------------------
# Sample assignment / substitution
# ---------------------------------------------------------------------------

def _assert_sample_assignable(sample: SampleUnit) -> None:
    if sample.terminal:
        labels = dict(SampleUnit.Status.choices)
        raise SampleUnavailableError(
            f"样品 {sample.barcode} 已处于终态「{labels.get(sample.status)}」，"
            "已消耗/损毁的样品不能重新分配"
        )
    if sample.status == SampleUnit.Status.RESERVED:
        active = sample.assignments.filter(state=SampleAssignment.State.ACTIVE).first()
        if active is not None:
            raise SampleUnavailableError(
                f"样品 {sample.barcode} 已分配给动作 {active.action_id}，不能重复分配"
            )
    if sample.status == SampleUnit.Status.WITHDRAWN:
        raise SampleUnavailableError(
            f"样品 {sample.barcode} 已取出，不能再分配给其他时间点"
        )


@transaction.atomic
def assign_original(action: PlannedAction, sample: SampleUnit, user,
                    note: str = "") -> SampleAssignment:
    if action.batch_id != sample.batch_id:
        raise ValidationError("样品与动作不属于同一批次")
    if action.assignments.filter(role=SampleAssignment.Role.ORIGINAL).exists():
        raise ValidationError("该动作已有原计划样品分配")
    _assert_sample_assignable(sample)

    assignment = SampleAssignment.objects.create(
        action=action, sample=sample,
        role=SampleAssignment.Role.ORIGINAL,
        created_by=user, note=note,
    )
    if sample.status == SampleUnit.Status.IN_STORAGE:
        sample.status = SampleUnit.Status.RESERVED
        sample.save(update_fields=["status"])
    return assignment


@transaction.atomic
def assign_substitute(action: PlannedAction, sample: SampleUnit, user,
                      reason: str = "", note: str = "") -> SampleAssignment:
    """Arrange a replacement sample for a missing/failed time point.

    Creates a NEW assignment; the failed one is marked FAILED and the planned
    action is left exactly as it was.
    """
    if action.batch_id != sample.batch_id:
        raise ValidationError("样品与动作不属于同一批次")
    if not reason:
        raise ValidationError("安排替代样品必须填写替代原因")

    # Latest prior assignment regardless of state: damage/misplacement may
    # already have failed it before the substitute is arranged.
    previous = action.assignments.order_by("-created_at").first()

    # Allowed even when no assignment ever existed (overdue open action).
    if previous is not None and previous.sample_id == sample.id:
        raise SampleUnavailableError("替代样品不能与失败样品相同")
    _assert_sample_assignable(sample)

    if previous is not None:
        if previous.state == SampleAssignment.State.ACTIVE:
            previous.state = SampleAssignment.State.FAILED
            previous.save(update_fields=["state"])
        # If the failed unit was merely reserved (never withdrawn), release it
        # back to storage only if it is physically usable; damage/misplacement
        # transitions below handle the unusable cases.
        prev_sample = previous.sample
        if prev_sample.status == SampleUnit.Status.RESERVED:
            prev_sample.status = SampleUnit.Status.IN_STORAGE
            prev_sample.save(update_fields=["status"])

    assignment = SampleAssignment.objects.create(
        action=action, sample=sample,
        role=SampleAssignment.Role.SUBSTITUTE,
        substitutes=previous, reason=reason,
        created_by=user, note=note,
    )
    if sample.status == SampleUnit.Status.IN_STORAGE:
        sample.status = SampleUnit.Status.RESERVED
        sample.save(update_fields=["status"])
    return assignment


# ---------------------------------------------------------------------------
# Withdrawal (actual sampling time)
# ---------------------------------------------------------------------------

@transaction.atomic
def withdraw_sample(assignment: SampleAssignment, withdrawn_at, user,
                    note: str = "") -> SampleUnit:
    """Record the ACTUAL withdrawal time. Never writes it to planned fields."""
    if assignment.state != SampleAssignment.State.ACTIVE:
        raise ValidationError("该分配已失败，不能执行取样")
    sample = assignment.sample
    if sample.status != SampleUnit.Status.RESERVED:
        raise ValidationError(
            f"样品当前状态为 {sample.get_status_display()}，无法登记取出")

    withdrawn_at = _aware(withdrawn_at)
    if withdrawn_at > timezone.now() + timedelta(minutes=1):
        raise ValidationError("实际取出时间不能晚于当前时间")

    sample.status = SampleUnit.Status.WITHDRAWN
    sample.withdrawn_at = withdrawn_at
    sample.withdrawn_by = user
    sample.save(update_fields=["status", "withdrawn_at", "withdrawn_by"])

    # Deviation vs the planned window → pending assessment → results freeze.
    action = assignment.action
    kind = None
    if withdrawn_at < _aware(action.window_start):
        kind = SampleException.Kind.EARLY_WITHDRAWAL
    elif withdrawn_at > _aware(action.window_end):
        kind = SampleException.Kind.LATE_WITHDRAWAL
    if kind:
        SampleException.objects.create(
            sample=sample, assignment=assignment, kind=kind,
            occurred_at=withdrawn_at,
            note=note or f"实际取出时间 {withdrawn_at:%Y-%m-%d %H:%M} 落在"
                         f"计划窗口 {action.window_start:%m-%d %H:%M}~"
                         f"{action.window_end:%m-%d %H:%M} 之外",
            recorded_by=user,
        )
    return sample


# ---------------------------------------------------------------------------
# Exceptions (misplaced / damaged / wrong location ...)
# ---------------------------------------------------------------------------

@transaction.atomic
def record_exception(sample: SampleUnit, kind: str, occurred_at, user,
                     note: str = "") -> SampleException:
    occurred_at = _aware(occurred_at)
    exception = SampleException.objects.create(
        sample=sample, kind=kind, occurred_at=occurred_at,
        note=note, recorded_by=user,
    )

    if kind == SampleException.Kind.DAMAGED:
        sample.status = SampleUnit.Status.DAMAGED
        sample.save(update_fields=["status"])
    elif kind in (SampleException.Kind.MISPLACED, SampleException.Kind.WRONG_LOCATION):
        sample.status = SampleUnit.Status.MISPLACED
        sample.save(update_fields=["status"])

    # Fail the sample's active assignment so the coordinator arranges a
    # substitute for the time point.
    if kind in (SampleException.Kind.DAMAGED, SampleException.Kind.MISPLACED):
        active = sample.assignments.filter(
            state=SampleAssignment.State.ACTIVE).first()
        if active is not None:
            active.state = SampleAssignment.State.FAILED
            active.save(update_fields=["state"])
            exception.assignment = active
            exception.save(update_fields=["assignment"])
    return exception


@transaction.atomic
def assess_exception(exception: SampleException, disposition: str, user,
                     note: str = "") -> SampleException:
    exception.disposition = disposition
    exception.assessed_by = user
    exception.assessed_at = timezone.now()
    exception.assessment_note = note
    exception.save(update_fields=[
        "disposition", "assessed_by", "assessed_at", "assessment_note",
    ])
    return exception


# ---------------------------------------------------------------------------
# Chamber events
# ---------------------------------------------------------------------------

@transaction.atomic
def record_chamber_event(event: ChamberEvent) -> ChamberEvent:
    event.started_at = _aware(event.started_at)
    if event.ended_at:
        event.ended_at = _aware(event.ended_at)
        if event.ended_at < event.started_at:
            raise ValidationError("事件结束时间不能早于开始时间")
    event.full_clean(exclude=["started_at", "ended_at"])
    event.save()
    return event


@transaction.atomic
def assess_chamber_event(event: ChamberEvent, assessment: str, user,
                         note: str = "") -> ChamberEvent:
    event.status = assessment
    event.assessed_by = user
    event.assessed_at = timezone.now()
    event.assessment_note = note
    event.save(update_fields=[
        "status", "assessed_by", "assessed_at", "assessment_note",
    ])
    return event


# ---------------------------------------------------------------------------
# Test results (analyst) and QA decisions
# ---------------------------------------------------------------------------

def _coerce_value(raw: str) -> Decimal | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        raise ValidationError(f"数值结果无法解析：{raw}")


@transaction.atomic
def submit_result(*, assignment: SampleAssignment, value: str | None = None,
                  value_text: str = "", unit: str = "", analyzed_at,
                  analyst) -> TestResult:
    """Analyst submits a measurement. Nothing about QA status is changed here."""
    if assignment.state != SampleAssignment.State.ACTIVE:
        raise ValidationError("不能对已失败的分配提交结果")
    sample = assignment.sample
    if sample.status != SampleUnit.Status.WITHDRAWN:
        raise ValidationError("只有已取出的样品才能登记检测结果")

    action = assignment.action
    if not action.test_item_id:
        raise ValidationError("动作缺少检测项目")
    if assignment.results.exists():
        raise ValidationError("该分配的检测结果已提交（一样品一结果，已消耗不可重用）")

    analyzed_at = _aware(analyzed_at)
    if analyzed_at > timezone.now() + timedelta(minutes=1):
        raise ValidationError("检测时间不能晚于当前时间")
    if analyzed_at < _aware(sample.withdrawn_at):
        raise ValidationError("检测时间不能早于实际取出时间")

    numeric = _coerce_value(value)
    if numeric is None and not value_text.strip():
        raise ValidationError("数值结果与文字结果至少填写一项")

    result = TestResult.objects.create(
        assignment=assignment,
        test_item=action.test_item,
        value=numeric,
        value_text=value_text.strip(),
        unit=unit or action.test_item.unit,
        analyzed_at=analyzed_at,
        analyst=analyst,
        qa_status=TestResult.QAStatus.PENDING,
    )
    # Consumed: terminal, never assignable again.
    sample.status = SampleUnit.Status.CONSUMED
    sample.save(update_fields=["status"])
    return result


@transaction.atomic
def decide_result(result: TestResult, qa_status: str, user,
                  note: str = "") -> TestResult:
    """QA include / exclude / mark additional study.

    Inclusion (or 'additional study') is refused while a deviation that
    affects this result is still awaiting impact assessment.  Exclusion is
    always permitted so QA can quarantine data immediately.
    """
    valid = {c for c, _ in TestResult.QAStatus.choices} - {TestResult.QAStatus.PENDING}
    if qa_status not in valid:
        raise ValidationError("无效的质量判定")

    # Imported here to avoid an import cycle at module load.
    from .engine import freeze_reasons_for_result

    reasons = freeze_reasons_for_result(result)
    if qa_status in (TestResult.QAStatus.INCLUDED, TestResult.QAStatus.ADDITIONAL) \
            and reasons:
        raise FrozenError(
            "结果处于冻结状态，存在待完成的影响评估，不能纳入或追加考察："
            + "；".join(reasons)
        )

    result.qa_status = qa_status
    result.decided_by = user
    result.decided_at = timezone.now()
    result.decision_note = note
    result.save(update_fields=[
        "qa_status", "decided_by", "decided_at", "decision_note",
    ])
    return result
