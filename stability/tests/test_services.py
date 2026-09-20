"""Tests for state transitions and business rules in services.py."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from stability import engine, services
from stability.exceptions import (
    FrozenError,
    SampleUnavailableError,
    ValidationError,
)
from stability.models import (
    ChamberEvent,
    PlannedAction,
    SampleAssignment,
    SampleException,
    SampleUnit,
    TestResult,
)

from . import factories


class PlanSnapshotTests(TestCase):
    def setUp(self):
        self.users = factories.make_three_users()
        self.chamber = factories.make_chamber()

    def test_plan_is_snapshot_and_immutable(self):
        protocol, cond = factories.make_protocol(self.users["coord"])
        batch = factories.make_batch(
            self.users["coord"], protocol, cond, self.chamber)
        action_count = batch.actions.count()
        self.assertGreater(action_count, 0)
        # Regeneration is refused; a new protocol version cannot rewrite this
        # batch's plan.
        with self.assertRaises(ValidationError):
            services.generate_plan(batch)
        self.assertEqual(batch.actions.count(), action_count)

    def test_planned_datetime_equals_storage_start_plus_offset(self):
        protocol, cond = factories.make_protocol(self.users["coord"])
        batch = factories.make_batch(
            self.users["coord"], protocol, cond, self.chamber)
        m3 = batch.actions.filter(point_code="M3").first()
        self.assertAlmostEqual(
            (m3.planned_datetime - batch.storage_start).total_seconds(),
            91 * 86400, delta=1)


class AssignmentTests(TestCase):
    def setUp(self):
        self.users = factories.make_three_users()
        self.protocol, self.cond = factories.make_protocol(self.users["coord"])
        self.chamber = factories.make_chamber()
        self.batch = factories.make_batch(
            self.users["coord"], self.protocol, self.cond, self.chamber,
            days_ago=100)
        self.action = PlannedAction.objects.get(
            batch=self.batch, point_code="M3", test_item__code="POT")

    def test_consumed_sample_cannot_be_reassigned(self):
        s1 = SampleUnit.objects.get(barcode="B-001-S01")
        other_action = PlannedAction.objects.get(
            batch=self.batch, point_code="M3", test_item__code="PH")
        services.assign_original(other_action, s1, self.users["coord"])
        t = other_action.planned_datetime
        services.withdraw_sample(other_action.active_assignment,
                                 t, self.users["coord"])
        services.submit_result(
            assignment=other_action.active_assignment, value="7.0",
            analyzed_at=t + timedelta(days=1), analyst=self.users["analyst"])
        s1.refresh_from_db()
        self.assertEqual(s1.status, SampleUnit.Status.CONSUMED)
        with self.assertRaises(SampleUnavailableError):
            services.assign_original(self.action, s1, self.users["coord"])

    def test_double_assignment_of_reserved_sample_rejected(self):
        s2 = SampleUnit.objects.get(barcode="B-001-S02")
        services.assign_original(self.action, s2, self.users["coord"])
        ph_action = PlannedAction.objects.get(
            batch=self.batch, point_code="M3", test_item__code="PH")
        with self.assertRaises(SampleUnavailableError):
            services.assign_original(ph_action, s2, self.users["coord"])

    def test_cross_batch_sample_rejected(self):
        other_batch = factories.make_batch(
            self.users["coord"], self.protocol, self.cond, self.chamber,
            number="B-002")
        foreign = other_batch.samples.first()
        with self.assertRaises(ValidationError):
            services.assign_original(self.action, foreign, self.users["coord"])


class SubstitutionTests(TestCase):
    def setUp(self):
        self.users = factories.make_three_users()
        self.protocol, self.cond = factories.make_protocol(self.users["coord"])
        self.chamber = factories.make_chamber()
        self.batch = factories.make_batch(
            self.users["coord"], self.protocol, self.cond, self.chamber,
            days_ago=100)
        self.action = PlannedAction.objects.get(
            batch=self.batch, point_code="M3", test_item__code="POT")

    def test_substitution_adds_assignment_without_changing_plan(self):
        original = SampleUnit.objects.get(barcode="B-001-S04")
        planned_before = (self.action.planned_datetime, self.action.window_start,
                          self.action.window_end)
        services.assign_original(self.action, original, self.users["coord"])
        services.record_exception(
            original, "damaged", timezone.now(), self.users["coord"], "碎瓶")

        replacement = SampleUnit.objects.get(barcode="B-001-S07")
        sub = services.assign_substitute(
            self.action, replacement, self.users["coord"], reason="damaged")

        self.action.refresh_from_db()
        self.assertEqual(
            (self.action.planned_datetime, self.action.window_start,
             self.action.window_end),
            planned_before)
        self.assertTrue(sub.is_substitute)
        self.assertEqual(sub.substitutes.sample_id, original.id)
        self.assertEqual(sub.substitutes.state, SampleAssignment.State.FAILED)
        self.assertEqual(self.action.active_assignment.id, sub.id)
        original.refresh_from_db()
        self.assertEqual(original.status, SampleUnit.Status.DAMAGED)

    def test_substitution_requires_reason(self):
        replacement = SampleUnit.objects.get(barcode="B-001-S07")
        with self.assertRaises(ValidationError):
            services.assign_substitute(
                self.action, replacement, self.users["coord"], reason="")

    def test_cannot_substitute_with_same_sample(self):
        s = SampleUnit.objects.get(barcode="B-001-S04")
        services.assign_original(self.action, s, self.users["coord"])
        with self.assertRaises(SampleUnavailableError):
            services.assign_substitute(
                self.action, s, self.users["coord"], reason="damaged")


class WithdrawalTests(TestCase):
    def setUp(self):
        self.users = factories.make_three_users()
        self.protocol, self.cond = factories.make_protocol(self.users["coord"])
        self.chamber = factories.make_chamber()
        self.batch = factories.make_batch(
            self.users["coord"], self.protocol, self.cond, self.chamber,
            days_ago=100)
        self.action = PlannedAction.objects.get(
            batch=self.batch, point_code="M3", test_item__code="POT")

    def test_actual_time_is_stored_separately_from_plan(self):
        s = SampleUnit.objects.get(barcode="B-001-S04")
        services.assign_original(self.action, s, self.users["coord"])
        actual = self.action.window_start - timedelta(days=3)
        services.withdraw_sample(self.action.active_assignment, actual,
                                 self.users["coord"])
        s.refresh_from_db()
        self.assertEqual(s.withdrawn_at, actual)
        self.action.refresh_from_db()
        self.assertNotEqual(self.action.planned_datetime, actual)

    def test_early_withdrawal_opens_pending_exception(self):
        s = SampleUnit.objects.get(barcode="B-001-S04")
        services.assign_original(self.action, s, self.users["coord"])
        services.withdraw_sample(
            self.action.active_assignment,
            self.action.window_start - timedelta(days=2), self.users["coord"])
        exc = s.exceptions.get()
        self.assertEqual(exc.kind, SampleException.Kind.EARLY_WITHDRAWAL)
        self.assertTrue(exc.pending)

    def test_withdraw_in_future_rejected(self):
        s = SampleUnit.objects.get(barcode="B-001-S04")
        services.assign_original(self.action, s, self.users["coord"])
        with self.assertRaises(ValidationError):
            services.withdraw_sample(
                self.action.active_assignment,
                timezone.now() + timedelta(days=1), self.users["coord"])

    def test_misplaced_sample_fails_assignment(self):
        s = SampleUnit.objects.get(barcode="B-001-S04")
        services.assign_original(self.action, s, self.users["coord"])
        services.record_exception(
            s, "misplaced", timezone.now(), self.users["coord"], "找不到")
        # No active assignment remains → coordinator can substitute.
        self.assertIsNone(self.action.assignments.filter(
            state=SampleAssignment.State.ACTIVE).first())
        self.assertEqual(
            self.action.assignments.get(role=SampleAssignment.Role.ORIGINAL).state,
            SampleAssignment.State.FAILED)


class ResultAndFreezeTests(TestCase):
    def setUp(self):
        self.users = factories.make_three_users()
        self.protocol, self.cond = factories.make_protocol(self.users["coord"])
        self.chamber = factories.make_chamber()
        self.batch = factories.make_batch(
            self.users["coord"], self.protocol, self.cond, self.chamber,
            days_ago=100)

    def _result_for(self, *, suffix="04", point="M3", item="POT"):
        action = PlannedAction.objects.get(
            batch=self.batch, point_code=point, test_item__code=item)
        sample = SampleUnit.objects.get(barcode=f"B-001-S{suffix}")
        services.assign_original(action, sample, self.users["coord"])
        t = action.planned_datetime
        services.withdraw_sample(action.active_assignment, t, self.users["coord"])
        return services.submit_result(
            assignment=action.active_assignment, value="0.9",
            analyzed_at=t + timedelta(days=1), analyst=self.users["analyst"])

    def test_result_starts_pending_and_consumes_sample(self):
        result = self._result_for()
        self.assertEqual(result.qa_status, TestResult.QAStatus.PENDING)
        self.assertEqual(result.sample.status, SampleUnit.Status.CONSUMED)

    def test_duplicate_result_rejected(self):
        action = PlannedAction.objects.get(
            batch=self.batch, point_code="M3", test_item__code="POT")
        sample = SampleUnit.objects.get(barcode="B-001-S04")
        services.assign_original(action, sample, self.users["coord"])
        t = action.planned_datetime
        services.withdraw_sample(action.active_assignment, t, self.users["coord"])
        services.submit_result(
            assignment=action.active_assignment, value="0.9",
            analyzed_at=t + timedelta(days=1), analyst=self.users["analyst"])
        with self.assertRaises(ValidationError):
            services.submit_result(
                assignment=action.active_assignment, value="0.9",
                analyzed_at=t + timedelta(days=2), analyst=self.users["analyst"])

    def test_pending_chamber_outage_freezes_inclusion(self):
        result = self._result_for()
        # Outage while the sample was resident (before its withdrawal).
        start = result.sample.withdrawn_at - timedelta(days=2)
        services.record_chamber_event(ChamberEvent(
            chamber=self.chamber,
            event_type=ChamberEvent.EventType.POWER_OUT,
            started_at=start, ended_at=start + timedelta(hours=3),
            recorded_by=self.users["coord"]))
        self.assertTrue(
            any("环境事件待影响评估" in r
                for r in engine.freeze_reasons_for_result(result)))
        with self.assertRaises(FrozenError):
            services.decide_result(result, "included", self.users["qa"])
        # Exclusion remains allowed so QA can quarantine immediately.
        services.decide_result(result, "excluded", self.users["qa"], "先排除")
        self.assertEqual(result.qa_status, TestResult.QAStatus.EXCLUDED)

    def test_inclusion_after_no_impact_assessment(self):
        result = self._result_for()
        start = result.sample.withdrawn_at - timedelta(days=2)
        event = services.record_chamber_event(ChamberEvent(
            chamber=self.chamber,
            event_type=ChamberEvent.EventType.POWER_OUT,
            started_at=start, ended_at=start + timedelta(hours=3),
            recorded_by=self.users["coord"]))
        with self.assertRaises(FrozenError):
            services.decide_result(result, "included", self.users["qa"])
        services.assess_chamber_event(event, "no_impact", self.users["qa"])
        services.decide_result(result, "included", self.users["qa"])
        self.assertEqual(result.qa_status, TestResult.QAStatus.INCLUDED)

    def test_pending_sample_exception_freezes_inclusion(self):
        result = self._result_for()
        SampleException.objects.create(
            sample=result.sample, kind="other",
            occurred_at=result.analyzed_at, note="标签存疑",
            disposition=SampleException.Disposition.PENDING,
            recorded_by=self.users["coord"])
        with self.assertRaises(FrozenError):
            services.decide_result(result, "included", self.users["qa"])
