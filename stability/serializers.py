"""Model -> dict serializers for the JSON API."""
from __future__ import annotations

from . import engine
from .models import (
    Batch,
    Chamber,
    ChamberEvent,
    PlannedAction,
    Protocol,
    SampleAssignment,
    SampleException,
    SampleUnit,
    TestResult,
)


def _dt(value):
    return value.isoformat() if value else None


def protocol_dict(p: Protocol) -> dict:
    return {
        "id": p.id,
        "code": p.code,
        "version": p.version,
        "title": p.title,
        "status": p.status,
        "status_display": p.get_status_display(),
        "effective_date": p.effective_date.isoformat(),
        "supersedes_id": p.supersedes_id,
    }


def chamber_dict(c: Chamber) -> dict:
    return {
        "id": c.id, "code": c.code, "name": c.name,
        "location": c.location, "condition_note": c.condition_note,
    }


def event_dict(e: ChamberEvent, *, now=None) -> dict:
    return {
        "id": e.id,
        "chamber_id": e.chamber_id,
        "chamber_code": e.chamber.code,
        "event_type": e.event_type,
        "event_type_display": e.get_event_type_display(),
        "started_at": _dt(e.started_at),
        "ended_at": _dt(e.ended_at),
        "observed_min_temp_c": str(e.observed_min_temp_c) if e.observed_min_temp_c is not None else None,
        "observed_max_temp_c": str(e.observed_max_temp_c) if e.observed_max_temp_c is not None else None,
        "description": e.description,
        "status": e.status,
        "status_display": e.get_status_display(),
        "pending": e.pending,
    }


def sample_dict(s: SampleUnit, *, now=None) -> dict:
    data = {
        "id": s.id,
        "barcode": s.barcode,
        "batch_id": s.batch_id,
        "status": s.status,
        "status_display": s.get_status_display(),
        "withdrawn_at": _dt(s.withdrawn_at),
        "terminal": s.terminal,
    }
    if now is not None:
        pending_exposure = engine.total_pending_exposure_seconds(s, now)
        data["pending_exposure_seconds"] = int(pending_exposure)
        data["pending_exposure_display"] = engine.format_duration(pending_exposure)
    return data


def assignment_dict(a: SampleAssignment) -> dict:
    return {
        "id": a.id,
        "action_id": a.action_id,
        "sample_barcode": a.sample.barcode,
        "role": a.role,
        "role_display": a.get_role_display(),
        "state": a.state,
        "state_display": a.get_state_display(),
        "substitutes_id": a.substitutes_id,
        "reason": a.reason,
        "reason_display": a.get_reason_display(),
    }


def exception_dict(x: SampleException) -> dict:
    return {
        "id": x.id,
        "sample_barcode": x.sample.barcode,
        "assignment_id": x.assignment_id,
        "kind": x.kind,
        "kind_display": x.get_kind_display(),
        "occurred_at": _dt(x.occurred_at),
        "note": x.note,
        "disposition": x.disposition,
        "disposition_display": x.get_disposition_display(),
        "pending": x.pending,
    }


def result_dict(r: TestResult, *, include_freeze: bool = True) -> dict:
    data = {
        "id": r.id,
        "assignment_id": r.assignment_id,
        "sample_barcode": r.sample.barcode,
        "test_item": r.test_item.code,
        "test_item_name": r.test_item.name,
        "value": str(r.value) if r.value is not None else None,
        "value_text": r.value_text,
        "unit": r.unit,
        "analyzed_at": _dt(r.analyzed_at),
        "qa_status": r.qa_status,
        "qa_status_display": r.get_qa_status_display(),
        "decided_by": r.decided_by.username if r.decided_by_id else None,
        "decided_at": _dt(r.decided_at),
        "decision_note": r.decision_note,
    }
    if include_freeze:
        reasons = engine.freeze_reasons_for_result(r)
        data["frozen"] = bool(reasons)
        data["freeze_reasons"] = reasons
    return data


def action_view_dict(v, *, now=None) -> dict:
    return {
        "action_id": v.action.id,
        "point_code": v.point_code,
        "test_item": v.test_item_code,
        "test_item_name": v.test_item_name,
        "planned_datetime": _dt(v.planned_datetime),
        "window_start": _dt(v.window_start),
        "window_end": _dt(v.window_end),
        "state": v.state,
        "state_display": v.state_display,
        "timing": v.timing,
        "timing_display": v.timing_display,
        "assignment": assignment_dict(v.assignment) if v.assignment else None,
        "sample_barcode": v.sample.barcode if v.sample else None,
        "result": result_dict(v.result) if v.result else None,
        "frozen": v.frozen,
        "freeze_reasons": v.freeze_reasons,
        "overdue": (now is not None and now > v.window_end
                   and v.state not in ("included", "additional")),
        "needs_substitute": v.needs_substitute,
        "can_withdraw": v.can_withdraw,
        "can_submit_result": v.can_submit_result,
        "can_substitute": v.can_substitute,
        "can_decide": v.can_decide,
    }


def batch_dict(b: Batch, *, now=None) -> dict:
    data = {
        "id": b.id,
        "batch_number": b.batch_number,
        "product_code": b.product_code,
        "product_name": b.product_name,
        "protocol": protocol_dict(b.protocol),
        "storage_condition_code": b.storage_condition.code,
        "chamber": chamber_dict(b.chamber),
        "storage_start": _dt(b.storage_start),
        "status": b.status,
        "status_display": b.get_status_display(),
        "plan_generated": b.plan_generated,
    }
    if now is not None:
        data["pending_exposure_seconds"] = int(
            engine.batch_pending_exposure_seconds(b, now))
        data["pending_exposure_display"] = engine.format_duration(
            data["pending_exposure_seconds"])
    return data
