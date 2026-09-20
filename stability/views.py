"""Server-rendered pages (display + action forms that call the JSON API)."""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from . import engine
from .models import (
    Batch,
    Chamber,
    ChamberEvent,
    SampleAssignment,
    SampleException,
    SampleUnit,
)
from .permissions import ROLE_ANALYST, ROLE_COORDINATOR, ROLE_QA, user_roles


def _role_flags(user) -> dict:
    roles = user_roles(user)
    return {
        "is_coordinator": ROLE_COORDINATOR in roles,
        "is_analyst": ROLE_ANALYST in roles,
        "is_qa": ROLE_QA in roles,
        "roles": sorted(roles),
    }


@login_required
def dashboard(request):
    now = timezone.now()
    events_by_chamber = engine.build_chamber_index(now)
    batches = list(
        Batch.objects.select_related("protocol", "chamber", "storage_condition")
    )
    rows = []
    for batch in batches:
        schedule = engine.batch_schedule(batch, now)
        overdue = [
            v for v in schedule
            if now > v.window_end and v.state not in ("included", "additional")
        ]
        frozen = [v for v in schedule if v.frozen]
        usable = len(engine.analyzable_results(batch))
        exposure = engine.batch_pending_exposure_seconds(
            batch, now, events_by_chamber)
        rows.append({
            "batch": batch,
            "overdue_count": len(overdue),
            "frozen_count": len(frozen),
            "usable_count": usable,
            "pending_exposure_seconds": int(exposure),
            "pending_exposure_display": engine.format_duration(exposure),
        })
    pending_events = ChamberEvent.objects.filter(
        status__in=(ChamberEvent.Assessment.OPEN,
                    ChamberEvent.Assessment.UNDER_REVIEW)
    ).select_related("chamber")
    pending_exceptions = SampleException.objects.filter(
        disposition=SampleException.Disposition.PENDING
    ).select_related("sample__batch")
    context = {
        "rows": rows,
        "now": now,
        "pending_events": pending_events,
        "pending_exceptions": pending_exceptions,
        **_role_flags(request.user),
    }
    return render(request, "stability/dashboard.html", context)


@login_required
def batch_detail(request, batch_id):
    now = timezone.now()
    batch = get_object_or_404(
        Batch.objects.select_related(
            "protocol", "chamber", "storage_condition"), pk=batch_id)
    schedule = engine.batch_schedule(batch, now)

    points: dict[str, list] = {}
    for view in schedule:
        points.setdefault(view.point_code, []).append(view)
    overdue = [
        v for v in schedule
        if now > v.window_end and v.state not in ("included", "additional")
    ]

    sample_rows = []
    for sample in batch.samples.prefetch_related("exceptions").order_by("barcode"):
        exposures = engine.exposure_events(sample, now)
        pending_seconds = engine.total_pending_exposure_seconds(sample, now)
        sample_rows.append({
            "sample": sample,
            "exposures": exposures,
            "pending_seconds": int(pending_seconds),
            "pending_display": engine.format_duration(pending_seconds),
        })

    timeline = engine.batch_timeline(batch, now)
    samples_available = (
        batch.samples.filter(status=SampleUnit.Status.IN_STORAGE).order_by("barcode")
    )

    context = {
        "batch": batch,
        "points": points,
        "overdue": overdue,
        "events": engine.chamber_events_for_batch(batch, now),
        "sample_rows": sample_rows,
        "timeline": timeline,
        "samples_available": samples_available,
        "sub_reasons": SampleAssignment.Reason.choices,
        "exc_kinds": SampleException.Kind.choices,
        "now": now,
        **_role_flags(request.user),
    }
    return render(request, "stability/batch_detail.html", context)


@login_required
def events_page(request):
    now = timezone.now()
    events = ChamberEvent.objects.select_related("chamber").order_by("-started_at")
    chambers = Chamber.objects.order_by("code")
    context = {
        "events": events, "chambers": chambers, "now": now,
        "event_types": ChamberEvent.EventType.choices,
        **_role_flags(request.user),
    }
    return render(request, "stability/events.html", context)


@login_required
def dataset_page(request):
    now = timezone.now()
    rows = engine.analyzable_dataset_rows()
    batches = Batch.objects.order_by("batch_number")
    context = {
        "rows": rows, "batches": batches, "now": now,
        "total": len(rows),
        **_role_flags(request.user),
    }
    return render(request, "stability/dataset.html", context)
