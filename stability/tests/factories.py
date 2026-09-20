"""Shared test-data builders."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.utils import timezone

from stability import services
from stability.models import (
    Batch,
    Chamber,
    ChamberEvent,
    Protocol,
    SampleUnit,
    StorageCondition,
    SamplingPoint,
    TestItem,
)
from stability.permissions import ROLE_ANALYST, ROLE_COORDINATOR, ROLE_QA


def make_user(username: str, role: str | None = None, *, staff=False) -> User:
    user = User.objects.create_user(username=username, password="pw")
    if staff:
        user.is_staff = True
        user.save(update_fields=["is_staff"])
    if role:
        user.groups.add(Group.objects.create(name=role)
                        if not Group.objects.filter(name=role).exists()
                        else Group.objects.get(name=role))
    return user


def make_three_users() -> dict[str, User]:
    Group.objects.get_or_create(name=ROLE_COORDINATOR)
    Group.objects.get_or_create(name=ROLE_ANALYST)
    Group.objects.get_or_create(name=ROLE_QA)
    return {
        "coord": make_user("coord", ROLE_COORDINATOR),
        "analyst": make_user("analyst", ROLE_ANALYST),
        "qa": make_user("qa", ROLE_QA),
    }


def make_protocol(created_by, *, code="P1", version=1, with_design=True):
    protocol = Protocol.objects.create(
        code=code, version=version, title=f"方案 {code}",
        status=Protocol.Status.ACTIVE,
        effective_date=timezone.now().date(), created_by=created_by,
    )
    cond = StorageCondition.objects.create(
        protocol=protocol, code="2-8C", label="2-8℃",
        target_temperature_c=Decimal("5"))
    if not with_design:
        return protocol, cond
    pot = TestItem.objects.create(
        protocol=protocol, code="POT", name="效价", unit="IU/mL")
    ph = TestItem.objects.create(
        protocol=protocol, code="PH", name="pH", unit="")
    specs = [
        ("M0", 0, 2, 2, [pot, ph]),
        ("M3", 91, 7, 7, [pot, ph]),
        ("M6", 182, 7, 14, [pot]),
    ]
    for idx, (code_, offset, before, after, items) in enumerate(specs):
        point = SamplingPoint.objects.create(
            protocol=protocol, code=code_, name=code_, offset_days=offset,
            window_before_days=before, window_after_days=after, order=idx)
        point.test_items.set(items)
    return protocol, cond


def make_chamber(code="INC-1"):
    return Chamber.objects.create(code=code, name="恒温箱", condition_note="5℃")


def make_batch(created_by, protocol, cond, chamber, *,
               number="B-001", days_ago=100, sample_count=12):
    start = timezone.now() - timedelta(days=days_ago)
    batch = Batch.objects.create(
        protocol=protocol, product_code="VX", product_name="疫苗",
        batch_number=number, storage_condition=cond, chamber=chamber,
        storage_start=start, created_by=created_by)
    for i in range(1, sample_count + 1):
        SampleUnit.objects.create(batch=batch, barcode=f"{number}-S{i:02d}")
    services.generate_plan(batch)
    return batch


def add_power_outage(chamber, recorded_by, *, hours_ago=10, duration_hours=3):
    start = timezone.now() - timedelta(hours=hours_ago)
    event = ChamberEvent(
        chamber=chamber, event_type=ChamberEvent.EventType.POWER_OUT,
        started_at=start, ended_at=start + timedelta(hours=duration_hours),
        observed_max_temp_c=Decimal("12.0"),
        description="短时断电", recorded_by=recorded_by)
    return services.record_chamber_event(event)


def full_workflow(users, batch, *, point="M3", item="POT", sample_suffix="04",
                  withdraw_delta_days=0):
    """Assign → withdraw → analyze a single action; return (view, result?)."""
    from stability.models import PlannedAction

    action = PlannedAction.objects.get(
        batch=batch, point_code=point, test_item__code=item)
    sample = SampleUnit.objects.get(barcode=f"{batch.batch_number}-S{sample_suffix}")
    services.assign_original(action, sample, users["coord"])
    t = action.planned_datetime + timedelta(days=withdraw_delta_days)
    services.withdraw_sample(action.active_assignment, t, users["coord"])
    sample.refresh_from_db()
    result = services.submit_result(
        assignment=action.active_assignment, value="0.90",
        analyzed_at=t + timedelta(days=1), analyst=users["analyst"])
    return action, sample, result
