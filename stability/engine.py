"""
Pure computation layer: timelines, overdue actions, excursion exposure,
freeze decisions and the currently analyzable data set.

Nothing here writes to the database; services.py owns state transitions.
All time handling is timezone-aware UTC.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from django.utils import timezone

from .models import (
    Batch,
    ChamberEvent,
    PlannedAction,
    SampleAssignment,
    SampleUnit,
    TestResult,
)

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if timezone.is_aware(value):
        return value
    return timezone.make_aware(value)


def overlap_seconds(
    a_start: datetime,
    a_end: datetime | None,
    b_start: datetime,
    b_end: datetime | None,
    now: datetime,
) -> float:
    """Overlap of [a_start, a_end|now) and [b_start, b_end|now) in seconds."""
    a_start, b_start = _aware(a_start), _aware(b_start)
    a_end = _aware(a_end) or now
    b_end = _aware(b_end) or now
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    return max(0.0, (hi - lo).total_seconds())


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds <= 0:
        return "0 分钟"
    hours, rem = divmod(seconds, 3600)
    days, hours = divmod(hours, 24)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes or not parts:
        parts.append(f"{minutes} 分钟")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Environmental exposure
# ---------------------------------------------------------------------------

def chamber_events_for_batch(
    batch: Batch,
    now: datetime | None = None,
    events_by_chamber: dict | None = None,
) -> list[ChamberEvent]:
    """Chamber events whose time range intersects the batch's storage life."""
    now = now or timezone.now()
    if events_by_chamber is not None and batch.chamber_id in events_by_chamber:
        events = events_by_chamber[batch.chamber_id]
    else:
        events = list(batch.chamber.events.all())
    storage_start = _aware(batch.storage_start)
    out = []
    for event in events:
        end = _aware(event.ended_at) or now
        if _aware(event.started_at) <= now and end >= storage_start:
            out.append(event)
    return sorted(out, key=lambda e: e.started_at)


def build_chamber_index(now: datetime | None = None) -> dict[int, list[ChamberEvent]]:
    """All chamber events grouped by chamber — avoids N+1 over a batch list."""
    index: dict[int, list[ChamberEvent]] = {}
    for event in ChamberEvent.objects.all():
        index.setdefault(event.chamber_id, []).append(event)
    return index


def sample_residence_end(sample: SampleUnit, now: datetime) -> datetime:
    """When the sample stopped being in the chamber (withdrawal) or now."""
    return _aware(sample.withdrawn_at) or now


def exposure_events(
    sample: SampleUnit,
    now: datetime | None = None,
    events_by_chamber: dict | None = None,
) -> list[dict]:
    """Chamber events the sample physically lived through, with exposure time.

    A sample withdrawn before an event starts (or entering the chamber after
    it ends) records zero exposure.
    """
    now = now or timezone.now()
    batch = sample.batch
    residence_start = _aware(batch.storage_start)
    residence_end = sample_residence_end(sample, now)
    out = []
    for event in chamber_events_for_batch(batch, now, events_by_chamber):
        seconds = overlap_seconds(
            residence_start, residence_end,
            event.started_at, event.ended_at, now,
        )
        if seconds > 0:
            out.append({
                "event": event,
                "exposure_seconds": seconds,
                "exposure_display": format_duration(seconds),
            })
    return out


def total_pending_exposure_seconds(
    sample: SampleUnit, now: datetime | None = None
) -> float:
    now = now or timezone.now()
    return sum(
        item["exposure_seconds"]
        for item in exposure_events(sample, now)
        if item["event"].pending
    )


def batch_pending_exposure_seconds(
    batch: Batch, now: datetime | None = None, events_by_chamber: dict | None = None
) -> float:
    """Pending excursion exposure overlapping the batch's time in the chamber."""
    now = now or timezone.now()
    total = 0.0
    for event in chamber_events_for_batch(batch, now, events_by_chamber):
        if not event.pending:
            continue
        total += overlap_seconds(
            batch.storage_start, now, event.started_at, event.ended_at, now,
        )
    return total


# ---------------------------------------------------------------------------
# Freeze logic
# ---------------------------------------------------------------------------

def freeze_reasons_for_sample(
    sample: SampleUnit,
    now: datetime | None = None,
    events_by_chamber: dict | None = None,
) -> list[str]:
    """Why results from this sample must stay out of the shelf-life data set.

    * any chamber excursion during the sample's residence pending assessment;
    * any pending sample exception (misplacement / damage / early withdrawal).

    Confirmed-impact assessments are *not* "freeze" reasons: QA then records
    the explicit EXCLUDED decision instead of waiting.
    """
    now = now or timezone.now()
    reasons: list[str] = []
    for item in exposure_events(sample, now, events_by_chamber):
        event = item["event"]
        if event.pending:
            reasons.append(
                f"环境事件待影响评估：{event.get_event_type_display()} "
                f"{event.started_at:%Y-%m-%d %H:%M}，暴露 {item['exposure_display']}"
            )
    for exc in sample.exceptions.all():
        if exc.pending:
            reasons.append(
                f"样品异常待影响评估：{exc.get_kind_display()}"
                f"（{exc.occurred_at:%Y-%m-%d %H:%M}）"
            )
    return reasons


def freeze_reasons_for_result(
    result: TestResult,
    now: datetime | None = None,
    events_by_chamber: dict | None = None,
) -> list[str]:
    return freeze_reasons_for_sample(result.sample, now, events_by_chamber)


def result_is_frozen(result: TestResult, now: datetime | None = None) -> bool:
    return bool(freeze_reasons_for_result(result, now))


# ---------------------------------------------------------------------------
# Per-action schedule (what can be done with each planned action)
# ---------------------------------------------------------------------------

@dataclass
class ActionView:
    action: PlannedAction
    point_code: str
    test_item_code: str
    test_item_name: str
    planned_datetime: datetime
    window_start: datetime
    window_end: datetime
    assignment: SampleAssignment | None = None
    sample: SampleUnit | None = None
    role: str = ""
    result: TestResult | None = None
    timing: str = "unknown"          # early / on_time / late / not_withdrawn
    timing_display: str = ""
    state: str = "open"              # see STATE_LABELS
    state_display: str = ""
    frozen: bool = False
    freeze_reasons: list[str] = field(default_factory=list)
    needs_substitute: bool = False
    can_submit_result: bool = False
    can_withdraw: bool = False
    can_substitute: bool = False
    can_decide: bool = False


STATE_LABELS = {
    "open": "未分配样品",
    "assigned": "已分配待取样",
    "withdrawn": "已取出待检测",
    "result_pending": "结果待质量判定",
    "included": "已纳入",
    "excluded": "已排除（可安排替代）",
    "additional": "追加考察",
    "failed": "原样品失败（待替代）",
}

TIMING_LABELS = {
    "early": "提前取出",
    "on_time": "窗口内取出",
    "late": "逾期取出",
    "not_withdrawn": "尚未取出",
}


def _timing_for(sample: SampleUnit | None, action: PlannedAction) -> str:
    # Timing is always classified against the *planned* window; the actual
    # timestamp is never relabelled as the planned one.
    if sample is None or sample.withdrawn_at is None:
        return "not_withdrawn"
    withdrawn = _aware(sample.withdrawn_at)
    if withdrawn < _aware(action.window_start):
        return "early"
    if withdrawn > _aware(action.window_end):
        return "late"
    return "on_time"


def build_action_view(
    action: PlannedAction,
    now: datetime | None = None,
    prefetched_assignments: list[SampleAssignment] | None = None,
    events_by_chamber: dict | None = None,
) -> ActionView:
    now = now or timezone.now()
    assignments = (
        prefetched_assignments
        if prefetched_assignments is not None
        else list(action.assignments.select_related("sample").order_by("created_at"))
    )
    active = next(
        (a for a in reversed(assignments) if a.state == SampleAssignment.State.ACTIVE),
        None,
    )

    view = ActionView(
        action=action,
        point_code=action.point_code,
        test_item_code=action.test_item.code,
        test_item_name=action.test_item.name,
        planned_datetime=action.planned_datetime,
        window_start=action.window_start,
        window_end=action.window_end,
    )

    if active is not None:
        view.assignment = active
        view.sample = active.sample
        view.role = active.role
        view.timing = _timing_for(active.sample, action)
        view.timing_display = TIMING_LABELS[view.timing]
        results = sorted(active.results.all(), key=lambda r: r.submitted_at, reverse=True)
        view.result = results[0] if results else None

    reasons = (
        freeze_reasons_for_sample(view.sample, now, events_by_chamber)
        if view.sample else []
    )
    view.frozen = bool(reasons)
    view.freeze_reasons = reasons

    # ---- state derivation ------------------------------------------------
    result = view.result
    if result is not None:
        view.state = result.qa_status  # pending / included / excluded / additional
        if view.state == TestResult.QAStatus.PENDING:
            view.state = "result_pending"
    elif active is None:
        view.state = "open"
    elif active.state == SampleAssignment.State.FAILED:
        view.state = "failed"
    elif view.sample and view.sample.withdrawn_at is not None:
        view.state = "withdrawn"
    else:
        view.state = "assigned"
    view.state_display = STATE_LABELS.get(view.state, view.state)

    # ---- overdue without a usable result ---------------------------------
    view.needs_substitute = (
        view.state in ("failed", "excluded", "open") and now > _aware(action.window_end)
    )

    # ---- currently executable actions ------------------------------------
    if view.state == "assigned":
        view.can_withdraw = view.sample.status == SampleUnit.Status.RESERVED
    if view.state == "withdrawn":
        view.can_submit_result = True
    view.can_substitute = view.state in ("open", "failed", "excluded")
    if result is not None and result.qa_status == TestResult.QAStatus.PENDING:
        # QA may open the decision form; service layer still blocks it while
        # the result is frozen pending an impact assessment.
        view.can_decide = True
    return view


def batch_schedule(
    batch: Batch, now: datetime | None = None
) -> list[ActionView]:
    now = now or timezone.now()
    events_by_chamber = build_chamber_index(now)
    actions = (
        batch.actions
        .select_related("sampling_point", "test_item")
        .prefetch_related(
            "assignments__sample__exceptions",
            "assignments__substitutes__sample",
            "assignments__results",
        )
        .order_by("planned_datetime", "test_item__code")
    )
    views = []
    for action in actions:
        assigns = list(action.assignments.all())  # served from prefetch cache
        views.append(build_action_view(action, now, assigns, events_by_chamber))
    return views


def overdue_actions(batch: Batch, now: datetime | None = None) -> list[ActionView]:
    now = now or timezone.now()
    return [
        v for v in batch_schedule(batch, now)
        if now > v.window_end and v.state not in ("included", "additional")
    ]


# ---------------------------------------------------------------------------
# Batch timeline
# ---------------------------------------------------------------------------

def batch_timeline(batch: Batch, now: datetime | None = None) -> list[dict]:
    """Chronological merge of plans, events, withdrawals, substitutions, results."""
    now = now or timezone.now()
    items: list[dict] = []

    items.append({
        "at": _aware(batch.storage_start), "kind": "start",
        "label": f"批次入库（考察零点），{batch.chamber.code}",
    })

    for event in chamber_events_for_batch(batch, now):
        items.append({
            "at": _aware(event.started_at), "kind": "event_start",
            "label": f"环境事件开始：{event.get_event_type_display()}（{event.chamber.code}）",
            "status": event.status, "pending": event.pending,
        })
        if event.ended_at:
            items.append({
                "at": _aware(event.ended_at), "kind": "event_end",
                "label": f"环境事件结束：{event.get_event_type_display()}",
                "status": event.status, "pending": event.pending,
            })

    for action in batch.actions.select_related("test_item", "sampling_point"):
        items.append({
            "at": _aware(action.planned_datetime), "kind": "planned",
            "label": (
                f"计划取样 {action.point_code} · {action.test_item.code}"
                f"（窗口 {action.window_start:%m-%d %H:%M} ~ {action.window_end:%m-%d %H:%M}）"
            ),
        })

    for sample in batch.samples.all():
        for exc in sample.exceptions.all():
            items.append({
                "at": _aware(exc.occurred_at), "kind": "exception",
                "label": (
                    f"样品 {sample.barcode}：{exc.get_kind_display()}"
                    + ("（待评估）" if exc.pending else "")
                ),
                "pending": exc.pending,
            })
        if sample.withdrawn_at:
            items.append({
                "at": _aware(sample.withdrawn_at), "kind": "withdrawal",
                "label": f"样品 {sample.barcode} 实际取出",
                "actual": True,
            })

    for assignment in (
        SampleAssignment.objects.filter(action__batch=batch)
        .select_related("sample", "action__sampling_point", "substitutes__sample",
                        "created_by")
    ):
        if assignment.is_substitute:
            prev_barcode = (
                assignment.substitutes.sample.barcode
                if assignment.substitutes_id else "?"
            )
            marker = (
                f"替代安排：{assignment.sample.barcode} 接替 {prev_barcode}"
                f"（{assignment.get_reason_display() or '原因未填'}；不改变原计划）"
            )
        else:
            marker = (
                f"样品分配：{assignment.sample.barcode} → "
                f"{assignment.action.point_code}"
            )
        items.append({
            "at": _aware(assignment.created_at), "kind": "assignment",
            "label": marker, "substitute": assignment.is_substitute,
        })

    for result in (
        TestResult.objects.filter(assignment__action__batch=batch)
        .select_related("assignment__sample", "test_item", "analyst")
    ):
        shown = result.value if result.value is not None else result.value_text
        items.append({
            "at": _aware(result.analyzed_at), "kind": "result",
            "label": (
                f"检测结果：{result.sample.barcode} {result.test_item.code} = {shown}"
                f" [{result.get_qa_status_display()}]"
            ),
            "actual": True,
        })

    return sorted(items, key=lambda x: (x["at"], 0 if x["kind"] == "start" else 1))


# ---------------------------------------------------------------------------
# Analyzable data set
# ---------------------------------------------------------------------------

def analyzable_results(batch: Batch | None = None, include_additional: bool = False):
    """Results currently usable for trend / shelf-life analysis.

    QA INCLUDED is required, and nothing with a *live* pending freeze may be
    used: a decision taken before a freshly reported power outage is
    automatically withheld until that assessment closes.
    """
    statuses = [TestResult.QAStatus.INCLUDED]
    if include_additional:
        statuses.append(TestResult.QAStatus.ADDITIONAL)
    qs = (
        TestResult.objects.filter(qa_status__in=statuses)
        .select_related(
            "assignment__sample__batch__protocol",
            "assignment__action__batch",
            "assignment__action__sampling_point",
            "assignment__sample__batch__chamber", "test_item", "decided_by",
        )
        .order_by(
            "assignment__sample__batch__batch_number",
            "assignment__action__planned_datetime",
            "test_item__code",
        )
    )
    if batch is not None:
        qs = qs.filter(assignment__action__batch=batch)

    now = timezone.now()
    events_by_chamber = build_chamber_index(now)
    live = []
    for result in qs:
        if not freeze_reasons_for_result(result, now, events_by_chamber):
            live.append(result)
    return live


def analyzable_dataset_rows(
    batch: Batch | None = None, include_additional: bool = False
) -> list[dict]:
    rows = []
    for result in analyzable_results(batch, include_additional):
        action = result.assignment.action
        planned_offset = (
            action.planned_datetime - _aware(action.batch.storage_start)
        ).days
        actual_age_days = (
            result.analyzed_at - _aware(action.batch.storage_start)
        ).days
        rows.append({
            "batch_id": action.batch_id,
            "batch_number": action.batch.batch_number,
            "product": action.batch.product_name or action.batch.product_code,
            "protocol": str(action.batch.protocol),
            "point_code": action.sampling_point.code,
            "planned_day": planned_offset,
            "actual_age_days": actual_age_days,
            "delta_days": actual_age_days - planned_offset,
            "sample_barcode": result.sample.barcode,
            "is_substitute": result.assignment.is_substitute,
            "test_item": result.test_item.code,
            "test_item_name": result.test_item.name,
            "value": result.value,
            "value_text": result.value_text,
            "unit": result.unit,
            "analyzed_at": result.analyzed_at,
            "qa_status": result.qa_status,
        })
    return rows
