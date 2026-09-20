"""
Seed a demonstration scenario and the three role accounts.

Users (password = username):
    coord   协调员
    analyst 分析员
    qa      质量人员

Scenario: protocol STAB-VAX v1 (2-8°C, points M0/M3/M6), chamber INC-01 with a
short power outage, three batches with early/on-time/late withdrawals,
damage, substitution and QA include/exclude decisions.
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand
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


class Command(BaseCommand):
    help = "创建角色账号并写入演示数据（断电/提前取出/替代/冻结/质量判定）"

    def handle(self, *args, **options):
        coord = self._user("coord", ROLE_COORDINATOR, is_staff=True)
        analyst = self._user("analyst", ROLE_ANALYST)
        qa = self._user("qa", ROLE_QA)

        now = timezone.now()
        zero = now - timedelta(days=110)  # M3 (91±7d) already past window

        # ---- protocol ----------------------------------------------------
        protocol, _ = Protocol.objects.get_or_create(
            code="STAB-VAX", version=1,
            defaults=dict(
                title="新冠灭活疫苗长期稳定性方案",
                status=Protocol.Status.ACTIVE,
                effective_date=(now - timedelta(days=200)).date(),
                created_by=coord,
            ),
        )
        cond, _ = StorageCondition.objects.get_or_create(
            protocol=protocol, code="2-8C",
            defaults=dict(label="2~8℃ 避光", target_temperature_c=Decimal("5")),
        )
        potency, _ = TestItem.objects.get_or_create(
            protocol=protocol, code="POT",
            defaults=dict(name="效价", unit="IU/mL",
                          specification="≥ 0.7 × 标示量"))
        ph, _ = TestItem.objects.get_or_create(
            protocol=protocol, code="PH",
            defaults=dict(name="pH 值", unit="", specification="6.5 ~ 7.5"))
        appearance, _ = TestItem.objects.get_or_create(
            protocol=protocol, code="APP",
            defaults=dict(name="外观", unit="", specification="乳白色混悬液"))

        point_specs = [
            ("M0", "0 月", 0, 2, 2, [potency, ph, appearance]),
            ("M3", "3 月", 91, 7, 7, [potency, ph, appearance]),
            ("M6", "6 月", 182, 7, 14, [potency, ph]),
        ]
        for code, name, offset, before, after, items in point_specs:
            point, created = SamplingPoint.objects.get_or_create(
                protocol=protocol, code=code,
                defaults=dict(name=name, offset_days=offset,
                              window_before_days=before, window_after_days=after,
                              order=offset),
            )
            if created:
                point.test_items.set(items)

        # ---- chambers + short power outages (one per batch so QA can
        # assess each excursion independently) ---------------------------
        # Outage hits two days BEFORE the M3 planned time, while the M3
        # samples are still resident in the chamber → their results are born
        # frozen and await QA's impact assessment.
        outage_start = zero + timedelta(days=89)
        chambers = {}
        for idx, number in enumerate(("B2026-001", "B2026-002", "B2026-003"), start=1):
            code = f"INC-{idx:02d}"
            chamber_obj, _ = Chamber.objects.get_or_create(
                code=code,
                defaults=dict(name=f"2~8℃ 恒温箱 {idx}",
                              location=f"稳定性考察室 {idx}",
                              condition_note="5℃ ± 3℃"),
            )
            chambers[number] = chamber_obj

        self._batch(protocol, cond, chambers["B2026-001"], "B2026-001", zero,
                    coord, analyst, qa, outage_start, mode="normal")
        self._batch(protocol, cond, chambers["B2026-002"], "B2026-002", zero,
                    coord, analyst, qa, outage_start, mode="damage")
        self._batch(protocol, cond, chambers["B2026-003"], "B2026-003", zero,
                    coord, analyst, qa, outage_start, mode="early")

        self.stdout.write(self.style.SUCCESS(
            "演示数据就绪。账号 coord/analyst/qa，密码与账号相同。"))

    # ------------------------------------------------------------------
    def _user(self, username: str, role: str, is_staff=False) -> User:
        group, _ = Group.objects.get_or_create(name=role)
        user, created = User.objects.get_or_create(
            username=username, defaults=dict(is_staff=is_staff,
                                             is_superuser=is_staff))
        if created:
            user.set_password(username)
            user.save()
        user.groups.add(group)
        return user

    def _batch(self, protocol, cond, chamber, number, zero, coord,
               analyst, qa, outage_start, mode):
        if Batch.objects.filter(batch_number=number).exists():
            return
        batch = Batch.objects.create(
            protocol=protocol, product_code="VX-19",
            product_name="新冠灭活疫苗 0.5mL",
            batch_number=number, storage_condition=cond, chamber=chamber,
            storage_start=zero, created_by=coord,
        )
        barcodes = [f"{number}-S{idx:02d}" for idx in range(1, 13)]
        for barcode in barcodes:
            SampleUnit.objects.create(batch=batch, barcode=barcode)
        services.generate_plan(batch)

        if not ChamberEvent.objects.filter(chamber=chamber).exists():
            services.record_chamber_event(ChamberEvent(
                chamber=chamber,
                event_type=ChamberEvent.EventType.POWER_OUT,
                started_at=outage_start,
                ended_at=outage_start + timedelta(hours=3, minutes=20),
                observed_max_temp_c=Decimal("11.8"),
                description="夜间短时断电，来电后温度恢复，监控报警 3h20m",
                recorded_by=coord,
            ))

        from stability.models import PlannedAction

        def action(point_code, item_code):
            return PlannedAction.objects.get(
                batch=batch, point_code=point_code, test_item__code=item_code)

        def sample(barcode):
            return SampleUnit.objects.get(barcode=barcode)

        # --- M0: all assigned on day 0, withdrawn on time, analyzed day 1
        for item_code in ("POT", "PH", "APP"):
            act = action("M0", item_code)
            idx = {"POT": 1, "PH": 2, "APP": 3}[item_code]
            s = sample(f"{number}-S{idx:02d}")
            services.assign_original(act, s, coord)
            services.withdraw_sample(
                act.active_assignment, zero + timedelta(hours=2), coord)
            result = services.submit_result(
                assignment=act.active_assignment,
                value={"POT": "0.98", "PH": "7.05", "APP": None}[item_code],
                value_text="" if item_code != "APP" else "符合规定",
                analyzed_at=zero + timedelta(days=1), analyst=analyst)
            services.decide_result(result, "included", qa, "M0 基线结果纳入")

        # --- M3: the interesting point ----------------------------------
        pot_m3 = action("M3", "POT")
        ph_m3 = action("M3", "PH")
        app_m3 = action("M3", "APP")

        m3_planned = pot_m3.planned_datetime
        # The power outage sits between M3 withdrawal and analysis for
        # samples still in the chamber.
        if mode == "early":
            # S04 pulled BEFORE the window opened (early) → deviation → its
            # result freezes; coordinator then arranges a substitute that
            # keeps the original planned point unchanged.
            s4 = sample(f"{number}-S04")
            services.assign_original(pot_m3, s4, coord)
            early_time = m3_planned - timedelta(days=12)
            services.withdraw_sample(
                pot_m3.active_assignment, early_time, coord,
                "月度取样前发现该样品已被提前取出")
            early_result = services.submit_result(
                assignment=pot_m3.active_assignment, value="0.93",
                analyzed_at=early_time + timedelta(days=1), analyst=analyst)
            # Early-withdrawal deviation is still pending → inclusion blocked.
            services.assign_substitute(
                pot_m3, sample(f"{number}-S07"), coord,
                reason="early_withdrawal",
                note="提前取出已检测，另取在窗口内的备用样品补做该时间点")
        else:
            s4 = sample(f"{number}-S04")
            services.assign_original(pot_m3, s4, coord)

        s5 = sample(f"{number}-S05")
        services.assign_original(ph_m3, s5, coord)

        if mode == "damage":
            # S04 found damaged before withdrawal → fail assignment, substitute
            services.record_exception(
                s4, "damaged", outage_start - timedelta(days=1), coord,
                "搬运中跌落，安瓿破裂")
            services.assign_substitute(
                pot_m3, sample(f"{number}-S08"), coord,
                reason="damaged", note="破损后安排同批备用样品")

        # Withdraw around M3 (after the outage, so in-chamber samples were
        # exposed to the 3h20m power outage while still stored).
        withdraw_time = m3_planned + timedelta(days=1)
        active_pot = pot_m3.active_assignment
        services.withdraw_sample(active_pot, withdraw_time, coord)
        services.withdraw_sample(ph_m3.active_assignment, withdraw_time, coord)

        pot_result = services.submit_result(
            assignment=active_pot, value="0.91",
            analyzed_at=withdraw_time + timedelta(days=1), analyst=analyst)
        ph_result = services.submit_result(
            assignment=ph_m3.active_assignment, value="7.02",
            analyzed_at=withdraw_time + timedelta(days=1), analyst=analyst)

        # QA tries to include → blocked while outage assessment is open.
        # In the normal batch, QA assesses the outage as no impact first;
        # other batches leave it pending to demonstrate the freeze.
        if mode == "normal":
            event = ChamberEvent.objects.get(chamber=chamber)
            services.assess_chamber_event(
                event, "no_impact", qa,
                "断电 3h20m，温度峰值 11.8℃，按累积热暴露评估可接受")
            services.decide_result(pot_result, "included", qa, "M3 效价纳入")
            services.decide_result(ph_result, "included", qa, "M3 pH 纳入")
        else:
            # QA may still exclude frozen data immediately.
            services.decide_result(ph_result, "excluded", qa,
                                   "先排除，等待断电影响评估后再定")
            # The early-withdrawn result stays pending (frozen): QA cannot
            # include it until that deviation is assessed.
            if mode == "early":
                assert early_result.qa_status == "pending"

        # APP M3 deliberately left un-withdrawn to show an overdue/open action
        # only in the 'early' batch.
        if mode != "early":
            s6 = sample(f"{number}-S06")
            services.assign_original(app_m3, s6, coord)
            # withdrawn late past the window end
            late_time = app_m3.window_end + timedelta(days=3)
            if late_time < timezone.now():
                services.withdraw_sample(app_m3.active_assignment, late_time, coord)
