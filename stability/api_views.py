"""
JSON API.

Authentication: Django session with standard CSRF protection. Browser pages
send the token via ``X-CSRFToken``; scripted clients read ``csrftoken`` from
the session cookie (obtained via GET on any page or /api/me/) and send it back
the same way. Authorization is the role layer in :mod:`stability.permissions`,
checked on every mutating endpoint.

Role matrix
-----------
coordinator : generate plan, assign / substitute samples, withdraw,
              record sample exceptions, record chamber events,
              create protocols / batches / samples.
analyst     : submit test results only.
qa          : assess chamber events & sample exceptions, and
              include / exclude / mark-additional test results.
reads       : any authenticated user.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from . import engine, serializers, services
from .exceptions import (
    FrozenError,
    SampleUnavailableError,
    StabilityError,
    ValidationError as DomainValidationError,
)
from .models import (
    Batch,
    Chamber,
    ChamberEvent,
    PlannedAction,
    Protocol,
    SampleAssignment,
    SampleException,
    SampleUnit,
    SamplingPoint,
    StorageCondition,
    TestItem,
    TestResult,
)
from .permissions import (
    ROLE_ANALYST,
    ROLE_COORDINATOR,
    ROLE_QA,
    parse_body,
    read_access,
    require_roles,
    user_roles,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _json_error(exc: Exception, status: int = 400):
    return JsonResponse({"error": type(exc).__name__, "detail": str(exc)},
                        status=status)


def endpoint(view_func):
    """Uniform domain-error mapping for a JSON endpoint.

    Authentication is handled by :func:`stability.permissions.read_access`
    and the ``require_roles`` decorators; Django's standard CSRF middleware
    protects all unsafe methods.
    """
    @read_access
    def wrapper(request, *args, **kwargs):
        try:
            return view_func(request, *args, **kwargs)
        except (DomainValidationError, StabilityError) as exc:
            status = 409 if isinstance(exc, (FrozenError, SampleUnavailableError)) else 400
            return _json_error(exc, status)
        except ValueError as exc:
            return _json_error(exc, 400)
    wrapper.__wrapped_endpoint__ = view_func
    return wrapper


def parse_dt(value, field: str):
    if not value:
        raise ValueError(f"缺少必填时间字段：{field}")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValueError(f"时间字段 {field} 不是合法 ISO 8601 时间：{value!r}")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def parse_decimal(value, field: str, allow_none=True):
    if value in (None, ""):
        if allow_none:
            return None
        raise ValueError(f"缺少必填数值字段：{field}")
    try:
        return Decimal(str(value))
    except Exception:
        raise ValueError(f"数值字段 {field} 无法解析：{value!r}")


def parse_date(value, field: str):
    if not value:
        raise ValueError(f"缺少必填日期字段：{field}")
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValueError(f"日期字段 {field} 不是合法 ISO 日期：{value!r}")


# ---------------------------------------------------------------------------
# identity / reference data
# ---------------------------------------------------------------------------

@ensure_csrf_cookie
@require_http_methods(["GET"])
@read_access
def me(request):
    roles = sorted(user_roles(request.user))
    return JsonResponse({
        "username": request.user.username,
        "roles": roles,
        "role_labels": roles,
        "permissions": {
            "can_coordinate": ROLE_COORDINATOR in roles,
            "can_submit_results": ROLE_ANALYST in roles,
            "can_qa_decide": ROLE_QA in roles,
        },
    })


@require_http_methods(["GET"])
@endpoint
def protocol_list(request):
    qs = Protocol.objects.select_related("supersedes").all()
    return JsonResponse({"results": [serializers.protocol_dict(p) for p in qs]})


@require_http_methods(["GET"])
@endpoint
def chamber_list(request):
    return JsonResponse({
        "results": [serializers.chamber_dict(c) for c in Chamber.objects.all()]
    })


@require_http_methods(["GET", "POST"])
@endpoint
def event_list(request):
    if request.method == "POST":
        return _event_create(request)
    now = timezone.now()
    events = ChamberEvent.objects.select_related("chamber").order_by("-started_at")
    chamber_id = request.GET.get("chamber_id")
    if chamber_id:
        events = events.filter(chamber_id=chamber_id)
    return JsonResponse({
        "results": [
            serializers.event_dict(e, now=now) for e in events
        ]
    })


@require_roles(ROLE_COORDINATOR)
def _event_create(request):
    data = parse_body(request)
    chamber = get_object_or_404(Chamber, pk=data.get("chamber_id"))
    event = ChamberEvent(
        chamber=chamber,
        event_type=data.get("event_type", ChamberEvent.EventType.OTHER),
        started_at=parse_dt(data.get("started_at"), "started_at"),
        ended_at=parse_dt(data["ended_at"], "ended_at") if data.get("ended_at") else None,
        observed_min_temp_c=parse_decimal(data.get("observed_min_temp_c"), "observed_min_temp_c"),
        observed_max_temp_c=parse_decimal(data.get("observed_max_temp_c"), "observed_max_temp_c"),
        description=data.get("description", ""),
        recorded_by=request.user,
    )
    services.record_chamber_event(event)
    return JsonResponse(serializers.event_dict(event), status=201)


@require_http_methods(["POST"])
@endpoint
def event_assess(request, event_id):
    return _event_assess(request, event_id)


@require_roles(ROLE_QA)
def _event_assess(request, event_id):
    data = parse_body(request)
    event = get_object_or_404(ChamberEvent, pk=event_id)
    assessment = data.get("assessment")
    if assessment not in dict(ChamberEvent.Assessment.choices):
        raise ValueError("assessment 必须是 open/under_review/no_impact/impact")
    services.assess_chamber_event(event, assessment, request.user,
                                  data.get("note", ""))
    return JsonResponse(serializers.event_dict(event))


# ---------------------------------------------------------------------------
# Protocols (coordinator creates; reads open to all)
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@endpoint
def protocol_create(request):
    return _protocol_create(request)


@require_roles(ROLE_COORDINATOR)
@transaction.atomic
def _protocol_create(request):
    data = parse_body(request)
    code = data.get("code")
    if not code:
        raise ValueError("缺少方案编号 code")
    version = int(data.get("version", 1))
    supersedes = None
    if data.get("supersedes_id"):
        supersedes = get_object_or_404(Protocol, pk=data["supersedes_id"])
        if supersedes.code != code:
            raise DomainValidationError("新版本必须与被替代版本使用同一方案编号")
        version = supersedes.version + 1
    protocol = Protocol.objects.create(
        code=code, version=version,
        title=data.get("title", code),
        status=Protocol.Status.DRAFT,
        effective_date=parse_date(
            data.get("effective_date")
            or timezone.now().date().isoformat(),
            "effective_date"),
        supersedes=supersedes,
        notes=data.get("notes", ""),
        created_by=request.user,
    )
    for cond in data.get("conditions", []):
        StorageCondition.objects.create(
            protocol=protocol,
            code=cond["code"], label=cond.get("label", cond["code"]),
            target_temperature_c=parse_decimal(
                cond.get("target_temperature_c", "5"), "target_temperature_c",
                allow_none=False),
            target_humidity_pct=parse_decimal(
                cond.get("target_humidity_pct"), "target_humidity_pct"),
        )
    item_by_code: dict[str, TestItem] = {}
    for item in data.get("test_items", []):
        item_by_code[item["code"]] = TestItem.objects.create(
            protocol=protocol, code=item["code"],
            name=item.get("name", item["code"]),
            method=item.get("method", ""), unit=item.get("unit", ""),
            specification=item.get("specification", ""),
        )
    for idx, point in enumerate(data.get("points", [])):
        p = SamplingPoint.objects.create(
            protocol=protocol, code=point["code"],
            name=point.get("name", point["code"]),
            offset_days=int(point["offset_days"]),
            window_before_days=int(point.get("window_before_days", 0)),
            window_after_days=int(point.get("window_after_days", 0)),
            order=int(point.get("order", idx)),
        )
        codes = point.get("test_item_codes", list(item_by_code))
        p.test_items.set([item_by_code[c] for c in codes])
    if data.get("activate"):
        services.activate_protocol(protocol)
    return JsonResponse(serializers.protocol_dict(protocol), status=201)


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------

@require_http_methods(["GET", "POST"])
@endpoint
def batch_list(request):
    if request.method == "POST":
        return _batch_create(request)
    return _batch_list_get(request)


def _batch_list_get(request):
    now = timezone.now()
    batches = (
        Batch.objects.select_related("protocol", "storage_condition", "chamber")
        .order_by("batch_number")
    )
    return JsonResponse({
        "results": [serializers.batch_dict(b, now=now) for b in batches]
    })


@require_roles(ROLE_COORDINATOR)
@transaction.atomic
def _batch_create(request):
    data = parse_body(request)
    protocol = get_object_or_404(Protocol, pk=data.get("protocol_id"))
    condition = get_object_or_404(
        StorageCondition, pk=data.get("storage_condition_id"))
    chamber = get_object_or_404(Chamber, pk=data.get("chamber_id"))
    batch = Batch.objects.create(
        protocol=protocol,
        product_code=data.get("product_code", ""),
        product_name=data.get("product_name", ""),
        batch_number=data["batch_number"],
        storage_condition=condition,
        chamber=chamber,
        storage_start=parse_dt(data.get("storage_start"), "storage_start"),
        created_by=request.user,
    )
    for barcode in data.get("sample_barcodes", []):
        SampleUnit.objects.create(batch=batch, barcode=barcode)
    if data.get("generate_plan"):
        services.generate_plan(batch)
    batch.refresh_from_db()
    return JsonResponse(serializers.batch_dict(batch, now=timezone.now()),
                        status=201)


@require_http_methods(["GET"])
@endpoint
def batch_detail(request, batch_id):
    batch = get_object_or_404(
        Batch.objects.select_related(
            "protocol", "storage_condition", "chamber"), pk=batch_id)
    now = timezone.now()
    return JsonResponse({
        "batch": serializers.batch_dict(batch, now=now),
        "schedule": [
            serializers.action_view_dict(v, now=now)
            for v in engine.batch_schedule(batch, now)
        ],
    })


@require_http_methods(["GET"])
@endpoint
def batch_schedule_view(request, batch_id):
    batch = get_object_or_404(Batch, pk=batch_id)
    now = timezone.now()
    return JsonResponse({
        "results": [
            serializers.action_view_dict(v, now=now)
            for v in engine.batch_schedule(batch, now)
        ]
    })


@require_http_methods(["GET"])
@endpoint
def batch_overdue_view(request, batch_id):
    batch = get_object_or_404(Batch, pk=batch_id)
    now = timezone.now()
    overdue = engine.overdue_actions(batch, now)
    return JsonResponse({
        "count": len(overdue),
        "results": [serializers.action_view_dict(v, now=now) for v in overdue],
    })


@require_http_methods(["GET"])
@endpoint
def batch_timeline_view(request, batch_id):
    batch = get_object_or_404(Batch, pk=batch_id)
    items = engine.batch_timeline(batch)
    return JsonResponse({
        "results": [
            {"at": item["at"].isoformat(),
             "kind": item["kind"],
             "label": item["label"],
             "pending": item.get("pending", False),
             "actual": item.get("actual", False),
             "substitute": item.get("substitute", False)}
            for item in items
        ]
    })


@require_http_methods(["GET"])
@endpoint
def batch_exposure_view(request, batch_id):
    batch = get_object_or_404(
        Batch.objects.select_related("chamber"), pk=batch_id)
    now = timezone.now()
    events = engine.chamber_events_for_batch(batch, now)
    samples = []
    for sample in batch.samples.order_by("barcode"):
        exposures = engine.exposure_events(sample, now)
        samples.append({
            "barcode": sample.barcode,
            "status": sample.status,
            "status_display": sample.get_status_display(),
            "pending_exposure_seconds": int(
                engine.total_pending_exposure_seconds(sample, now)),
            "exposures": [
                {
                    "event_id": item["event"].id,
                    "event_type": item["event"].event_type,
                    "event_type_display": item["event"].get_event_type_display(),
                    "status": item["event"].status,
                    "pending": item["event"].pending,
                    "exposure_seconds": int(item["exposure_seconds"]),
                    "exposure_display": item["exposure_display"],
                }
                for item in exposures
            ],
        })
    return JsonResponse({
        "batch_number": batch.batch_number,
        "pending_exposure_seconds": int(
            engine.batch_pending_exposure_seconds(batch, now)),
        "events": [
            {
                "id": e.id, "event_type": e.event_type,
                "event_type_display": e.get_event_type_display(),
                "started_at": e.started_at.isoformat(),
                "ended_at": e.ended_at.isoformat() if e.ended_at else None,
                "status": e.status, "pending": e.pending,
            } for e in events
        ],
        "samples": samples,
    })


@require_http_methods(["POST"])
@endpoint
def batch_generate_plan(request, batch_id):
    return _batch_generate_plan(request, batch_id)


@require_roles(ROLE_COORDINATOR)
def _batch_generate_plan(request, batch_id):
    batch = get_object_or_404(Batch, pk=batch_id)
    created = services.generate_plan(batch)
    return JsonResponse({"created_actions": len(created)}, status=201)


# ---------------------------------------------------------------------------
# Actions: assign / substitute
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
@endpoint
def action_detail(request, action_id):
    action = get_object_or_404(
        PlannedAction.objects.select_related(
            "sampling_point", "test_item", "batch"), pk=action_id)
    now = timezone.now()
    view = engine.build_action_view(action, now)
    return JsonResponse(serializers.action_view_dict(view, now=now))


@require_http_methods(["POST"])
@endpoint
def action_assign(request, action_id):
    return _action_assign(request, action_id)


@require_roles(ROLE_COORDINATOR)
def _action_assign(request, action_id):
    action = get_object_or_404(PlannedAction, pk=action_id)
    data = parse_body(request)
    sample = get_object_or_404(SampleUnit, barcode=data.get("sample_barcode"))
    assignment = services.assign_original(
        action, sample, request.user, data.get("note", ""))
    return JsonResponse(serializers.assignment_dict(assignment), status=201)


@require_http_methods(["POST"])
@endpoint
def action_substitute(request, action_id):
    return _action_substitute(request, action_id)


@require_roles(ROLE_COORDINATOR)
def _action_substitute(request, action_id):
    action = get_object_or_404(PlannedAction, pk=action_id)
    data = parse_body(request)
    sample = get_object_or_404(SampleUnit, barcode=data.get("sample_barcode"))
    assignment = services.assign_substitute(
        action, sample, request.user,
        reason=data.get("reason", ""), note=data.get("note", ""))
    return JsonResponse(serializers.assignment_dict(assignment), status=201)


# ---------------------------------------------------------------------------
# Withdrawal / exceptions
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@endpoint
def assignment_withdraw(request, assignment_id):
    return _assignment_withdraw(request, assignment_id)


@require_roles(ROLE_COORDINATOR)
def _assignment_withdraw(request, assignment_id):
    assignment = get_object_or_404(SampleAssignment, pk=assignment_id)
    data = parse_body(request)
    withdrawn_at = parse_dt(
        data.get("withdrawn_at", timezone.now().isoformat()), "withdrawn_at")
    sample = services.withdraw_sample(
        assignment, withdrawn_at, request.user, data.get("note", ""))
    return JsonResponse(serializers.sample_dict(sample, now=timezone.now()))


@require_http_methods(["POST"])
@endpoint
def sample_exception(request, sample_id):
    return _sample_exception(request, sample_id)


@require_roles(ROLE_COORDINATOR)
def _sample_exception(request, sample_id):
    sample = get_object_or_404(SampleUnit, pk=sample_id)
    data = parse_body(request)
    kind = data.get("kind")
    if kind not in dict(SampleException.Kind.choices):
        raise ValueError("kind 取值非法")
    occurred_at = parse_dt(
        data.get("occurred_at", timezone.now().isoformat()), "occurred_at")
    exc = services.record_exception(
        sample, kind, occurred_at, request.user, data.get("note", ""))
    return JsonResponse(serializers.exception_dict(exc), status=201)


@require_http_methods(["POST"])
@endpoint
def exception_assess(request, exception_id):
    return _exception_assess(request, exception_id)


@require_roles(ROLE_QA)
def _exception_assess(request, exception_id):
    exc = get_object_or_404(SampleException, pk=exception_id)
    data = parse_body(request)
    disposition = data.get("disposition")
    if disposition not in dict(SampleException.Disposition.choices):
        raise ValueError("disposition 必须是 pending/no_impact/impact")
    services.assess_exception(
        exc, disposition, request.user, data.get("note", ""))
    return JsonResponse(serializers.exception_dict(exc))


# ---------------------------------------------------------------------------
# Results: analyst submits, QA decides
# ---------------------------------------------------------------------------

@require_http_methods(["POST"])
@endpoint
def result_submit(request, assignment_id):
    return _result_submit(request, assignment_id)


@require_roles(ROLE_ANALYST)
def _result_submit(request, assignment_id):
    assignment = get_object_or_404(SampleAssignment, pk=assignment_id)
    data = parse_body(request)
    result = services.submit_result(
        assignment=assignment,
        value=str(data["value"]) if data.get("value") is not None else None,
        value_text=data.get("value_text", ""),
        unit=data.get("unit", ""),
        analyzed_at=parse_dt(
            data.get("analyzed_at", timezone.now().isoformat()), "analyzed_at"),
        analyst=request.user,
    )
    return JsonResponse(serializers.result_dict(result), status=201)


@require_http_methods(["POST"])
@endpoint
def result_decide(request, result_id):
    return _result_decide(request, result_id)


@require_roles(ROLE_QA)
def _result_decide(request, result_id):
    result = get_object_or_404(TestResult, pk=result_id)
    data = parse_body(request)
    result = services.decide_result(
        result, data.get("qa_status", ""), request.user,
        data.get("note", ""))
    return JsonResponse(serializers.result_dict(result))


@require_http_methods(["GET"])
@endpoint
def result_list(request):
    qs = TestResult.objects.select_related(
        "assignment__action__batch", "assignment__sample", "test_item")
    batch_id = request.GET.get("batch_id")
    if batch_id:
        qs = qs.filter(assignment__action__batch_id=batch_id)
    only = request.GET.get("status")
    if only:
        qs = qs.filter(qa_status=only)
    return JsonResponse({
        "results": [serializers.result_dict(r) for r in qs]
    })


# ---------------------------------------------------------------------------
# Analyzable data set
# ---------------------------------------------------------------------------

@require_http_methods(["GET"])
@endpoint
def dataset(request):
    include_additional = request.GET.get("include_additional") in ("1", "true", "yes")
    batch = None
    if request.GET.get("batch_id"):
        batch = get_object_or_404(Batch, pk=request.GET["batch_id"])
    rows = engine.analyzable_dataset_rows(batch, include_additional)
    return JsonResponse({"count": len(rows), "results": rows})
