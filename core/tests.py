"""业务规则与权限测试。"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from core import services
from core.models import (
    ActionKind,
    ActionStatus,
    Assay,
    AssayResult,
    Batch,
    Chamber,
    Disposition,
    EnvironmentEvent,
    EventType,
    ImpactDecision,
    Product,
    Protocol,
    ProtocolVersion,
    Role,
    ScheduledAction,
    SampleStatus,
    SampleUnit,
    StorageCondition,
    TimePointDefinition,
    UserProfile,
)


class StabilityTestBase(APITestCase):
    def setUp(self):
        self.coord = self._user("coord", Role.COORDINATOR)
        self.analyst = self._user("analyst", Role.ANALYST)
        self.qa = self._user("qa", Role.QA)

        self.cond = StorageCondition.objects.create(
            code="2-8C", name="冷藏", temp_low=2, temp_high=8)
        self.chamber = Chamber.objects.create(
            code="INC-01", name="1号箱", storage_condition=self.cond)
        self.product = Product.objects.create(code="P1", name="示例疫苗")
        self.protocol = Protocol.objects.create(
            code="SP-01", title="稳定性方案", product=self.product)
        self.pv = ProtocolVersion.objects.create(
            protocol=self.protocol, version_no="1.0",
            status=ProtocolVersion.Status.EFFECTIVE,
            storage_condition=self.cond, created_by=self.coord)
        self.tp0 = TimePointDefinition.objects.create(
            protocol_version=self.pv, code="0M", offset_days=0)
        self.tp3 = TimePointDefinition.objects.create(
            protocol_version=self.pv, code="3M", offset_days=91)
        self.assay = Assay.objects.create(code="PH", name="pH", lower_spec=6.5, upper_spec=7.5)
        self.tp0.assays.add(self.assay)
        self.tp3.assays.add(self.assay)

        self.zero = timezone.now() - timedelta(days=100)
        self.batch = Batch.objects.create(
            product=self.product, batch_no="B001", protocol_version=self.pv,
            chamber=self.chamber, manufacturing_date=self.zero.date(),
            created_by=self.coord)
        services.schedule_actions_for_batch(self.batch, created_by=self.coord)
        self.s1 = SampleUnit.objects.create(
            barcode="B001-1", batch=self.batch, chamber=self.chamber, placed_at=self.zero)
        self.s2 = SampleUnit.objects.create(
            barcode="B001-2", batch=self.batch, chamber=self.chamber, placed_at=self.zero)
        self.s3 = SampleUnit.objects.create(
            barcode="B001-3", batch=self.batch, chamber=self.chamber, placed_at=self.zero)

    def _user(self, name, role):
        u = User.objects.create_user(username=name, password="pw")
        UserProfile.objects.create(user=u, role=role)
        return u

    def action(self, code):
        return self.batch.actions.get(timepoint_code=code)


class PlanAndSamplingTests(StabilityTestBase):
    def test_schedule_generated_per_version(self):
        acts = {a.timepoint_code: a for a in self.batch.actions.filter(kind=ActionKind.SCHEDULED)}
        self.assertEqual(set(acts), {"0M", "3M"})
        a = acts["3M"]
        self.assertEqual(a.window_start, a.planned_at - timedelta(hours=48))
        self.assertEqual(a.window_end, a.planned_at + timedelta(hours=48))

    def test_effective_version_timepoints_locked(self):
        self.client.force_authenticate(self.coord)
        r = self.client.post("/api/timepoints/", {
            "protocol_version": self.pv.id, "code": "6M", "offset_days": 182})
        self.assertEqual(r.status_code, status.HTTP_409_CONFLICT)
        # 草案版本可改
        pv2 = ProtocolVersion.objects.create(
            protocol=self.protocol, version_no="2.0",
            status=ProtocolVersion.Status.DRAFT, storage_condition=self.cond)
        r = self.client.post("/api/timepoints/", {
            "protocol_version": pv2.id, "code": "1M", "offset_days": 30})
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)

    def test_actual_pull_never_overwrites_plan(self):
        a = self.action("0M")
        services.assign_sample(a, self.s1, user=self.coord)
        planned = a.planned_at
        actual = a.window_start - timedelta(hours=12)  # 提前 12 小时
        services.record_sampling(a, actual, self.coord, "提前取出")
        a.refresh_from_db()
        self.assertEqual(a.planned_at, planned)  # 计划时间不变
        self.assertEqual(a.sampling_event.pulled_at, actual)
        self.assertTrue(a.sampling_event.early)
        self.assertEqual(a.sample.status, SampleStatus.CONSUMED)

    def test_consumed_sample_cannot_be_reassigned(self):
        a0 = self.action("0M")
        services.assign_sample(a0, self.s1, user=self.coord)
        services.record_sampling(a0, timezone.now(), self.coord)
        a3 = self.action("3M")
        with self.assertRaises(services.ServiceError):
            services.assign_sample(a3, self.s1, user=self.coord)
        # API 层同样拒绝
        self.client.force_authenticate(self.coord)
        r = self.client.post(f"/api/actions/{a3.id}/assign/", {"sample_id": self.s1.id})
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_frozen_action_blocks_sampling(self):
        a = self.action("3M")
        services.assign_sample(a, self.s2, user=self.coord)
        services.mark_sample_problem(self.s2, SampleStatus.MISPLACED, "找不到")
        a.refresh_from_db()
        self.assertEqual(a.status, ActionStatus.FROZEN)
        with self.assertRaises(services.ServiceError):
            services.record_sampling(a, timezone.now(), self.coord)
        # 找回后解冻，可继续取样
        services.mark_sample_found(self.s2)
        a.refresh_from_db()
        self.assertEqual(a.status, ActionStatus.PENDING)

    def test_overdue_detection(self):
        a = self.action("3M")  # zero 是 100 天前，3M(91天)窗口已过
        self.assertTrue(a.is_overdue)
        self.client.force_authenticate(self.coord)
        r = self.client.get("/api/actions-overdue/")
        self.assertIn(a.id, [x["id"] for x in r.json()])


class ProblemFreezeTests(StabilityTestBase):
    def test_damaged_freezes_action_and_existing_results(self):
        # 先取样并提交结果，再标记破损 -> 结果冻结
        a = self.action("0M")
        services.assign_sample(a, self.s1, user=self.coord)
        services.record_sampling(a, timezone.now(), self.coord)
        result = services.submit_result(a, self.assay, "7.1", self.analyst)
        services.mark_sample_problem(self.s1, SampleStatus.DAMAGED, "破损")
        result.refresh_from_db()
        self.assertTrue(result.is_frozen)
        # 冻结结果 QA 不能直接处置
        with self.assertRaises(services.ServiceError):
            services.dispose_result(result, Disposition.INCLUDED, self.qa)

    def test_damaged_cannot_be_found(self):
        services.mark_sample_problem(self.s3, SampleStatus.DAMAGED, "破损")
        with self.assertRaises(services.ServiceError):
            services.mark_sample_found(self.s3)


class TemperatureExcursionTests(StabilityTestBase):
    def _event(self):
        return services.log_environment_event(
            self.chamber, EventType.POWER_OUTAGE,
            started_at=timezone.now() - timedelta(hours=3),
            ended_at=timezone.now() - timedelta(hours=1),
            min_temp=7.5, max_temp=11.0,
            description="断电两小时", recorded_by=self.qa)

    def test_excursion_creates_exposures_and_freezes(self):
        # s1 分配到 3M 动作但未取样
        a3 = self.action("3M")
        services.assign_sample(a3, self.s1, user=self.coord)
        event = self._event()
        exposures = {e.sample_id: e for e in event.exposures.all()}
        self.assertIn(self.s1.id, exposures)
        self.assertGreater(exposures[self.s1.id].minutes_exposed, 60)
        a3.refresh_from_db()
        self.assertEqual(a3.status, ActionStatus.FROZEN)
        self.assertEqual(a3.freeze_reason, "temp_excursion")

    def test_release_unfreezes(self):
        a3 = self.action("3M")
        services.assign_sample(a3, self.s1, user=self.coord)
        event = self._event()
        exp = event.exposures.get(sample=self.s1)
        services.assess_impact(exp, ImpactDecision.RELEASE, self.qa, "影响可接受")
        a3.refresh_from_db()
        self.assertEqual(a3.status, ActionStatus.PENDING)
        self.assertFalse(self.s1.has_open_exposure)

    def test_exclude_withdraws_sample_and_voids_action(self):
        a3 = self.action("3M")
        services.assign_sample(a3, self.s1, user=self.coord)
        # 再给 s2 一个已取样结果
        a0 = self.action("0M")
        services.assign_sample(a0, self.s2, user=self.coord)
        services.record_sampling(a0, timezone.now(), self.coord)
        result = services.submit_result(a0, self.assay, "7.0", self.analyst)

        event = self._event()
        services.assess_impact(
            event.exposures.get(sample=self.s1), ImpactDecision.EXCLUDE, self.qa, "排除")
        services.assess_impact(
            event.exposures.get(sample=self.s2), ImpactDecision.EXCLUDE, self.qa, "排除")

        a3.refresh_from_db()
        self.s1.refresh_from_db()
        result.refresh_from_db()
        self.assertEqual(a3.status, ActionStatus.VOID)
        self.assertEqual(self.s1.status, SampleStatus.WITHDRAWN)
        self.assertEqual(result.disposition, Disposition.EXCLUDED)

    def test_consumed_sample_exposure_overlaps_until_pull(self):
        a0 = self.action("0M")
        services.assign_sample(a0, self.s1, user=self.coord)
        # 事件 3h 前开始；样品在事件开始 30 分钟后取走 -> 仅暴露 30 分钟
        pulled = timezone.now() - timedelta(hours=2, minutes=30)
        services.record_sampling(a0, pulled, self.coord)
        # 事件从 3h 前持续到 1h 前
        event = self._event()
        exp = event.exposures.get(sample=self.s1)
        self.assertLessEqual(exp.minutes_exposed, 40)
        self.assertGreaterEqual(exp.minutes_exposed, 25)

    def test_double_assessment_rejected(self):
        event = self._event()
        exp = event.exposures.get(sample=self.s1)
        services.assess_impact(exp, ImpactDecision.RELEASE, self.qa)
        with self.assertRaises(services.ServiceError):
            services.assess_impact(exp, ImpactDecision.EXCLUDE, self.qa)


class ReplacementTests(StabilityTestBase):
    def test_replacement_keeps_original_intact(self):
        a3 = self.action("3M")  # 已逾期
        original_planned = a3.planned_at
        repl = services.create_replacement(
            a3, self.s2, self.coord,
            planned_at=timezone.now(), note="原样品破损")
        a3.refresh_from_db()
        # 原计划行时间不变，替代关系挂在新行上
        self.assertEqual(a3.planned_at, original_planned)
        self.assertEqual(repl.kind, ActionKind.REPLACEMENT)
        self.assertEqual(repl.replaces_id, a3.id)
        self.assertEqual(repl.sample_id, self.s2.id)
        self.assertEqual(list(a3.replacements.all()), [repl])
        # 替代样品仍需正常取样，时间如实记录
        services.record_sampling(repl, timezone.now(), self.coord)
        self.assertEqual(self.s2.status, SampleStatus.CONSUMED)

    def test_cannot_replace_with_consumed_sample(self):
        a0 = self.action("0M")
        services.assign_sample(a0, self.s1, user=self.coord)
        services.record_sampling(a0, timezone.now(), self.coord)
        a3 = self.action("3M")
        with self.assertRaises(services.ServiceError):
            services.create_replacement(a3, self.s1, self.coord, planned_at=timezone.now())

    def test_cannot_replace_open_window_action(self):
        # 0M 若仍在窗口内不允许替代（构造一个未来动作）
        future = Batch.objects.create(
            product=self.product, batch_no="B002", protocol_version=self.pv,
            chamber=self.chamber, manufacturing_date=timezone.now().date(),
            created_by=self.coord)
        services.schedule_actions_for_batch(future, created_by=self.coord)
        a = future.actions.get(timepoint_code="0M")
        s = SampleUnit.objects.create(barcode="B002-1", batch=future, chamber=self.chamber)
        with self.assertRaises(services.ServiceError):
            services.create_replacement(a, s, self.coord, planned_at=timezone.now())


class ResultAndDispositionTests(StabilityTestBase):
    def _pulled_action_with_result(self):
        a = self.action("0M")
        services.assign_sample(a, self.s1, user=self.coord)
        services.record_sampling(a, timezone.now(), self.coord)
        return a, services.submit_result(a, self.assay, "7.1", self.analyst)

    def test_analyst_submits_qa_disposes(self):
        a, result = self._pulled_action_with_result()
        self.assertEqual(result.disposition, Disposition.PENDING)
        self.assertFalse(result.usable_for_trend)
        services.dispose_result(result, Disposition.INCLUDED, self.qa, "合格")
        result.refresh_from_db()
        self.assertTrue(result.usable_for_trend)

    def test_duplicate_result_rejected(self):
        a, _ = self._pulled_action_with_result()
        with self.assertRaises(services.ServiceError):
            services.submit_result(a, self.assay, "7.2", self.analyst)

    def test_assay_outside_timepoint_scope_rejected(self):
        a = self.action("0M")
        services.assign_sample(a, self.s1, user=self.coord)
        services.record_sampling(a, timezone.now(), self.coord)
        other = Assay.objects.create(code="X", name="其他项目")
        with self.assertRaises(services.ServiceError):
            services.submit_result(a, other, "v", self.analyst)

    def test_trend_dataset_only_included_unfrozen(self):
        _, r1 = self._pulled_action_with_result()
        services.dispose_result(r1, Disposition.INCLUDED, self.qa)
        a2 = self.action("3M")
        services.assign_sample(a2, self.s2, user=self.coord)
        services.record_sampling(a2, timezone.now(), self.coord)
        r2 = services.submit_result(a2, self.assay, "6.9", self.analyst)
        services.dispose_result(r2, Disposition.EXCLUDED, self.qa)  # 排除
        r3_action = services.create_replacement(
            a2, self.s3, self.coord, planned_at=timezone.now())
        services.record_sampling(r3_action, timezone.now(), self.coord)
        r3 = services.submit_result(r3_action, self.assay, "7.0", self.analyst)
        services.dispose_result(r3, Disposition.INCLUDED, self.qa)  # 替代结果纳入

        self.client.force_authenticate(self.analyst)
        data = self.client.get("/api/trend-dataset/").json()
        values = {(row["timepoint_code"], row["kind"]): row["value"] for row in data["results"]}
        self.assertEqual(values[("0M", "scheduled")], "7.1")
        self.assertEqual(values[("3M", "replacement")], "7.0")
        self.assertEqual(data["count"], 2)
        self.assertNotIn(("3M", "scheduled"), values)
        # 行中计划与实际时间同时给出
        row = [r for r in data["results"] if r["kind"] == "replacement"][0]
        self.assertIsNotNone(row["planned_at"])
        self.assertIsNotNone(row["pulled_at"])


class PermissionTests(StabilityTestBase):
    endpoints_locked = [
        ("post", "/api/chambers/", {"code": "X", "name": "x",
                                    "storage_condition": None}),
    ]

    def test_anonymous_rejected(self):
        r = self.client.get("/api/batches/")
        self.assertEqual(r.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_analyst_cannot_assign_or_dispose(self):
        a = self.action("0M")
        self.client.force_authenticate(self.analyst)
        r = self.client.post(f"/api/actions/{a.id}/assign/", {"sample_id": self.s1.id})
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

        services.assign_sample(a, self.s1, user=self.coord)
        services.record_sampling(a, timezone.now(), self.coord)
        result = services.submit_result(a, self.assay, "7.1", self.analyst)
        r = self.client.post(f"/api/results/{result.id}/dispose/",
                             {"disposition": "included"})
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_coordinator_cannot_submit_results(self):
        a = self.action("0M")
        services.assign_sample(a, self.s1, user=self.coord)
        services.record_sampling(a, timezone.now(), self.coord)
        self.client.force_authenticate(self.coord)
        r = self.client.post("/api/results/",
                             {"action_id": a.id, "assay_id": self.assay.id, "value": "7.1"})
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

    def test_qa_cannot_create_batch_but_can_assess(self):
        self.client.force_authenticate(self.qa)
        r = self.client.post("/api/batches/", {
            "product": self.product.id, "batch_no": "BX",
            "protocol_version": self.pv.id, "chamber": self.chamber.id,
            "manufacturing_date": timezone.now().date()})
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)

        # QA 可以登记环境事件
        r = self.client.post("/api/events/", {
            "chamber": self.chamber.id, "event_type": EventType.TEMP_EXCURSION,
            "started_at": (timezone.now() - timedelta(hours=2)).isoformat(),
            "ended_at": timezone.now().isoformat(),
            "max_temp": "10.0"})
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        event = EnvironmentEvent.objects.get()
        exposure = event.exposures.first()
        r = self.client.post(f"/api/exposures/{exposure.id}/assess/",
                             {"decision": "release", "comment": "可接受"})
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_analyst_cannot_assess(self):
        event = services.log_environment_event(
            self.chamber, EventType.POWER_OUTAGE,
            started_at=timezone.now() - timedelta(hours=2),
            ended_at=timezone.now(), max_temp=10, recorded_by=self.qa)
        exp = event.exposures.first()
        self.client.force_authenticate(self.analyst)
        r = self.client.post(f"/api/exposures/{exp.id}/assess/", {"decision": "release"})
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)


class TimelineApiTests(StabilityTestBase):
    def test_timeline_payload(self):
        a3 = self.action("3M")
        repl = services.create_replacement(
            a3, self.s2, self.coord, planned_at=timezone.now())
        self.client.force_authenticate(self.coord)
        data = self.client.get(f"/api/batches/{self.batch.id}/timeline/").json()
        self.assertEqual(len(data["actions"]), 3)  # 0M, 3M, 替代
        orig = next(a for a in data["actions"] if a["timepoint_code"] == "3M"
                    and a["kind"] == "scheduled")
        self.assertEqual(orig["replacements"][0]["id"], repl.id)
        self.assertEqual(orig["replacements"][0]["sample_barcode"], "B001-2")
