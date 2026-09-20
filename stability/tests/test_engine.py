"""Tests for the pure computation layer: exposure, timing, dataset."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from stability import engine, services
from stability.models import ChamberEvent, PlannedAction, SampleUnit

from . import factories


class ExposureTests(TestCase):
    def setUp(self):
        self.users = factories.make_three_users()
        self.protocol, self.cond = factories.make_protocol(self.users["coord"])
        self.chamber = factories.make_chamber()
        # batch entered storage 30 days ago
        self.batch = factories.make_batch(
            self.users["coord"], self.protocol, self.cond, self.chamber,
            days_ago=30)

    def test_no_events_means_zero_exposure(self):
        sample = self.batch.samples.first()
        self.assertEqual(engine.exposure_events(sample), [])
        self.assertEqual(engine.total_pending_exposure_seconds(sample), 0.0)

    def test_sample_exposed_to_outage_while_in_chamber(self):
        start = timezone.now() - timedelta(hours=5)
        services.record_chamber_event(ChamberEvent(
            chamber=self.chamber,
            event_type=ChamberEvent.EventType.POWER_OUT,
            started_at=start, ended_at=start + timedelta(hours=2),
            recorded_by=self.users["coord"]))
        sample = self.batch.samples.first()
        exposures = engine.exposure_events(sample)
        self.assertEqual(len(exposures), 1)
        # sample never withdrawn → full 2h overlap
        self.assertAlmostEqual(exposures[0]["exposure_seconds"],
                               2 * 3600, delta=5)
        self.assertEqual(exposures[0]["exposure_display"], "2 小时")

    def test_sample_withdrawn_before_event_has_zero_exposure(self):
        # withdraw a sample 8 hours ago; outage is the last 2 hours
        sample = self.batch.samples.first()
        action = PlannedAction.objects.filter(
            batch=self.batch, test_item__code="POT").first()
        services.assign_original(action, sample, self.users["coord"])
        withdraw_time = timezone.now() - timedelta(hours=8)
        # Force-withdraw through the service; M0 window relative to storage
        # start 30d ago — 8h ago is long after M0 window → late deviation,
        # which is fine for this calculation test.
        services.withdraw_sample(action.active_assignment,
                                 withdraw_time, self.users["coord"])
        sample.refresh_from_db()

        start = timezone.now() - timedelta(hours=2)
        services.record_chamber_event(ChamberEvent(
            chamber=self.chamber,
            event_type=ChamberEvent.EventType.POWER_OUT,
            started_at=start, ended_at=start + timedelta(hours=1),
            recorded_by=self.users["coord"]))
        self.assertEqual(engine.exposure_events(sample), [])

    def test_partial_overlap_counts_only_shared_time(self):
        # Sample withdrawn one hour into a 3-hour outage.
        sample = self.batch.samples.first()
        action = PlannedAction.objects.filter(
            batch=self.batch, point_code="M0", test_item__code="POT").first()
        services.assign_original(action, sample, self.users["coord"])
        outage_start = timezone.now() - timedelta(hours=4)
        services.record_chamber_event(ChamberEvent(
            chamber=self.chamber,
            event_type=ChamberEvent.EventType.POWER_OUT,
            started_at=outage_start,
            ended_at=outage_start + timedelta(hours=3),
            recorded_by=self.users["coord"]))
        withdraw = outage_start + timedelta(hours=1)
        services.withdraw_sample(action.active_assignment,
                                 withdraw, self.users["coord"])
        sample.refresh_from_db()
        exposures = engine.exposure_events(sample)
        self.assertEqual(len(exposures), 1)
        self.assertAlmostEqual(
            exposures[0]["exposure_seconds"], 3600, delta=5)

    def test_ongoing_event_exposure_grows_until_now(self):
        start = timezone.now() - timedelta(hours=3, minutes=30)
        services.record_chamber_event(ChamberEvent(
            chamber=self.chamber,
            event_type=ChamberEvent.EventType.TEMP_EXCURSION,
            started_at=start, ended_at=None,
            recorded_by=self.users["coord"]))
        sample = self.batch.samples.first()
        exposures = engine.exposure_events(sample)
        self.assertEqual(len(exposures), 1)
        self.assertGreater(exposures[0]["exposure_seconds"], 3 * 3600)

    def test_event_on_other_chamber_is_ignored(self):
        other = factories.make_chamber("INC-OTHER")
        factories.add_power_outage(other, self.users["coord"])
        sample = self.batch.samples.first()
        self.assertEqual(engine.exposure_events(sample), [])


class FormatDurationTests(TestCase):
    def test_hours_and_minutes(self):
        self.assertEqual(engine.format_duration(3 * 3600 + 20 * 60),
                         "3 小时 20 分钟")

    def test_days_hours_minutes(self):
        self.assertEqual(engine.format_duration(2 * 86400 + 5 * 3600),
                         "2 天 5 小时")

    def test_zero(self):
        self.assertEqual(engine.format_duration(0), "0 分钟")


class TimingTests(TestCase):
    def setUp(self):
        self.users = factories.make_three_users()
        self.protocol, self.cond = factories.make_protocol(self.users["coord"])
        self.chamber = factories.make_chamber()
        self.batch = factories.make_batch(
            self.users["coord"], self.protocol, self.cond, self.chamber,
            days_ago=100)

    def _view(self, point="M3", item="POT"):
        action = PlannedAction.objects.get(
            batch=self.batch, point_code=point, test_item__code=item)
        return engine.build_action_view(action)

    def test_open_action_state(self):
        view = self._view()
        self.assertEqual(view.state, "open")
        self.assertTrue(view.can_substitute)
        self.assertFalse(view.can_withdraw)

    def test_early_on_time_late_classification(self):
        action = PlannedAction.objects.get(
            batch=self.batch, point_code="M3", test_item__code="POT")
        sample = SampleUnit.objects.get(barcode="B-001-S04")
        services.assign_original(action, sample, self.users["coord"])

        services.withdraw_sample(
            action.active_assignment,
            action.window_start - timedelta(days=1), self.users["coord"])
        view = engine.build_action_view(action)
        self.assertEqual(view.timing, "early")
        self.assertTrue(any(x.kind == "early_withdrawal"
                            for x in sample.exceptions.all()))

    def test_withdrawn_state(self):
        action, sample, _ = factories.full_workflow(
            self.users, self.batch, withdraw_delta_days=0)
        view = engine.build_action_view(action)
        self.assertEqual(view.timing, "on_time")
        self.assertEqual(view.state, "result_pending")

    def test_overdue_detection(self):
        # M6 planned ~82 days in the future, M3 ~9 days in the past.
        overdue = engine.overdue_actions(self.batch)
        codes = {(v.point_code, v.test_item_code) for v in overdue}
        self.assertIn(("M3", "POT"), codes)
        self.assertNotIn(("M6", "POT"), codes)

    def test_included_action_is_not_overdue(self):
        factories.full_workflow(self.users, self.batch)
        result = self.batch.actions.get(
            point_code="M3", test_item__code="POT").active_assignment.results.get()
        services.decide_result(result, "included", self.users["qa"])
        overdue = engine.overdue_actions(self.batch)
        self.assertFalse(any(
            v.point_code == "M3" and v.test_item_code == "POT" for v in overdue))


class DatasetTests(TestCase):
    def setUp(self):
        self.users = factories.make_three_users()
        self.protocol, self.cond = factories.make_protocol(self.users["coord"])
        self.chamber = factories.make_chamber()
        self.batch = factories.make_batch(
            self.users["coord"], self.protocol, self.cond, self.chamber,
            days_ago=100)

    def test_only_included_results_appear(self):
        _, _, included = factories.full_workflow(self.users, self.batch,
                                                 sample_suffix="04")
        services.decide_result(included, "included", self.users["qa"])
        _, _, pending = factories.full_workflow(
            self.users, self.batch, point="M3", item="PH", sample_suffix="05")
        rows = engine.analyzable_dataset_rows(self.batch)
        items = {(r["point_code"], r["test_item"]) for r in rows}
        self.assertEqual(items, {("M3", "POT")})

    def test_previously_included_is_withdrawn_during_new_outage(self):
        _, _, result = factories.full_workflow(self.users, self.batch)
        services.decide_result(result, "included", self.users["qa"])
        self.assertEqual(len(engine.analyzable_results(self.batch)), 1)

        # New outage after inclusion — dated while the sample was still in
        # the chamber (it was withdrawn ~9 days ago) and only reported now →
        # result freezes out of the live data set even though QA included it.
        factories.add_power_outage(self.chamber, self.users["coord"],
                                   hours_ago=15 * 24, duration_hours=3)
        self.assertEqual(len(engine.analyzable_results(self.batch)), 0)

        # QA assesses no impact → it becomes usable again.
        event = ChamberEvent.objects.get()
        services.assess_chamber_event(event, "no_impact", self.users["qa"])
        self.assertEqual(len(engine.analyzable_results(self.batch)), 1)

    def test_dataset_reports_planned_and_actual_age_separately(self):
        action, _, result = factories.full_workflow(
            self.users, self.batch, withdraw_delta_days=4)
        services.decide_result(result, "included", self.users["qa"])
        rows = engine.analyzable_dataset_rows(self.batch)
        row = rows[0]
        self.assertEqual(row["planned_day"], 91)
        self.assertEqual(row["actual_age_days"], 91 + 4 + 1)  # analyzed day
        self.assertEqual(row["delta_days"], 5)
