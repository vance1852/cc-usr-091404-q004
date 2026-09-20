"""生成演示数据：用户、方案版本、批次样品、断电偏离、提前取样、替代与 QA 处置。

运行：python manage.py seed_demo
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from core import services
from core.models import (
    Assay,
    Batch,
    Chamber,
    Disposition,
    EventType,
    ImpactDecision,
    Product,
    Protocol,
    ProtocolVersion,
    Role,
    SampleUnit,
    StorageCondition,
    TimePointDefinition,
    UserProfile,
)
from core.services import ServiceError


def dt(date, hour=9, minute=0):
    return timezone.make_aware(timezone.datetime(date[0], date[1], date[2], hour, minute))


class Command(BaseCommand):
    help = "创建演示场景（幂等：已存在则跳过）"

    def handle(self, *args, **options):
        now = timezone.now()

        # ---- 用户与角色 ----
        users = {}
        for uname, role, first in [
            ("coord_zhang", Role.COORDINATOR, "张协调"),
            ("analyst_li", Role.ANALYST, "李分析"),
            ("qa_wang", Role.QA, "王质量"),
        ]:
            user, created = User.objects.get_or_create(
                username=uname, defaults={"first_name": first, "is_staff": True}
            )
            if created:
                user.set_password("demo1234")
                user.save()
            UserProfile.objects.get_or_create(user=user, defaults={"role": role})
            users[role] = user

        # ---- 主数据 ----
        cond, _ = StorageCondition.objects.get_or_create(
            code="2-8C",
            defaults={"name": "冷藏 2-8℃", "temp_low": 2, "temp_high": 8,
                      "humidity_low": None, "humidity_high": None},
        )
        chamber, _ = Chamber.objects.get_or_create(
            code="INC-01", defaults={"name": "1号恒温箱", "storage_condition": cond,
                                      "location": "稳定性库房A"}
        )
        chamber2, _ = Chamber.objects.get_or_create(
            code="INC-02", defaults={"name": "2号恒温箱", "storage_condition": cond,
                                      "location": "稳定性库房A"}
        )
        product, _ = Product.objects.get_or_create(
            code="VAX-01", defaults={"name": "重组新冠疫苗（示例）", "dosage_form": "注射液"}
        )
        assays = {}
        for code, name, unit, lo, hi in [
            ("APR", "外观", "", None, None),
            ("PH", "pH", "", 6.5, 7.5),
            ("POT", "效价", "IU/mL", 80, 125),
            ("CON", "抗原含量", "μg/mL", 90, 110),
        ]:
            assays[code], _ = Assay.objects.get_or_create(
                code=code, defaults={"name": name, "unit": unit,
                                      "lower_spec": lo, "upper_spec": hi}
            )

        # ---- 方案与生效版本 ----
        protocol, _ = Protocol.objects.get_or_create(
            code="P-STAB-001", defaults={"title": "VAX-01 长期稳定性试验方案",
                                          "product": product}
        )
        pv, pv_created = ProtocolVersion.objects.get_or_create(
            protocol=protocol, version_no="1.0",
            defaults={"status": ProtocolVersion.Status.EFFECTIVE,
                      "storage_condition": cond, "default_pull_window_hours": 48,
                      "effective_date": now.date() - timedelta(days=200),
                      "created_by": users[Role.COORDINATOR]},
        )
        if pv_created:
            tps = []
            for code, days in [("0M", 0), ("1M", 30), ("3M", 91), ("6M", 183), ("12M", 365)]:
                tp = TimePointDefinition.objects.create(
                    protocol_version=pv, code=code, offset_days=days,
                    window_before_hours=48, window_after_hours=48,
                )
                tp.assays.set(list(assays.values()))
                tps.append(tp)

        # ---- 批次（零点 2026-06-01；3M≈09-01 已逾期，6M 在未来）----
        zero = dt((2026, 6, 1))
        batch, b_created = Batch.objects.get_or_create(
            batch_no="VAX-2601",
            defaults={"product": product, "protocol_version": pv, "chamber": chamber,
                      "manufacturing_date": zero.date(),
                      "created_by": users[Role.COORDINATOR]},
        )
        if b_created:
            services.schedule_actions_for_batch(batch, created_by=users[Role.COORDINATOR])

        samples = {}
        if b_created:
            for i in range(1, 9):
                s = SampleUnit.objects.create(
                    barcode=f"VAX-2601-S{i:02d}", batch=batch, chamber=chamber,
                    placed_at=zero,
                )
                samples[i] = s

            def action(tp_code):
                return batch.actions.get(timepoint_code=tp_code)

            # 0M：按时取样，分析员提交结果，QA 全部纳入
            a0 = action("0M")
            services.assign_sample(a0, samples[1], user=users[Role.COORDINATOR])
            services.record_sampling(a0, zero, users[Role.COORDINATOR])
            for code, val in [("APR", "符合规定"), ("PH", "7.1"), ("POT", "102"), ("CON", "101")]:
                r = services.submit_result(a0, assays[code], val, users[Role.ANALYST])
                services.dispose_result(r, Disposition.INCLUDED, users[Role.QA], "0月基线正常")

            # 1M：样品提前于窗口开始前 20 小时取出（实际时间如实记录，early=True）
            a1 = action("1M")
            services.assign_sample(a1, samples[2], user=users[Role.COORDINATOR])
            early_pull = a1.window_start - timedelta(hours=20)
            services.record_sampling(a1, early_pull, users[Role.COORDINATOR], "取样日冲突提前取出")
            for code, val in [("APR", "符合规定"), ("PH", "7.0"), ("POT", "99"), ("CON", "98")]:
                r = services.submit_result(a1, assays[code], val, users[Role.ANALYST])
                services.dispose_result(
                    r, Disposition.INCLUDED, users[Role.QA],
                    "虽提前取出但在技术容差内，纳入趋势分析"
                )

            # 3M：样品 3 破损 -> 冻结；安排样品 5 为替代
            a3 = action("3M")
            services.assign_sample(a3, samples[3], user=users[Role.COORDINATOR])
            services.mark_sample_problem(samples[3], "damaged", "运输途中跌落，内包装破损")

            repl = services.create_replacement(
                a3, samples[5], users[Role.COORDINATOR],
                planned_at=a3.planned_at + timedelta(days=3),
                note="原样品破损，安排替代",
            )
            services.record_sampling(
                repl, repl.planned_at + timedelta(hours=6), users[Role.COORDINATOR]
            )
            for code, val in [("APR", "符合规定"), ("PH", "7.0"), ("POT", "97"), ("CON", "99")]:
                services.submit_result(repl, assays[code], val, users[Role.ANALYST])
            # QA 接受替代结果并纳入
            for r in repl.results.all():
                services.dispose_result(r, Disposition.INCLUDED, users[Role.QA],
                                         "替代样品结果有效，纳入")

            # ---- 断电事件（2026-09-10 02:00-05:20，最高 11.4℃）----
            event = services.log_environment_event(
                chamber, EventType.POWER_OUTAGE,
                started_at=dt((2026, 9, 10), 2, 0),
                ended_at=dt((2026, 9, 10), 5, 20),
                min_temp=7.8, max_temp=11.4,
                description="夜间配电检修导致短时断电，箱温一度超标",
                recorded_by=users[Role.QA],
            )
            # QA 评估：对样品6（在箱备用）放行；对样品7排除（靠近出风口温度最高）
            exps = {e.sample_id: e for e in event.exposures.all()}
            services.assess_impact(
                exps[samples[6].id], ImpactDecision.RELEASE, users[Role.QA],
                "暴露 200 分钟、最高 11.4℃，累积热影响可接受，放行"
            )
            services.assess_impact(
                exps[samples[7].id], ImpactDecision.EXCLUDE, users[Role.QA],
                "靠近出风口，局部温度更高，预防性排除该单元"
            )
            # 样品 8 的暴露保持未决 —— 仪表盘上可见“等待影响评估”

        # 第二个批次：全部按计划尚未取样，用于展示干净时间轴
        batch2, b2_created = Batch.objects.get_or_create(
            batch_no="VAX-2602",
            defaults={"product": product, "protocol_version": pv, "chamber": chamber2,
                      "manufacturing_date": zero.date(),
                      "created_by": users[Role.COORDINATOR]},
        )
        if b2_created:
            services.schedule_actions_for_batch(batch2, created_by=users[Role.COORDINATOR])
            for i in range(1, 7):
                SampleUnit.objects.create(
                    barcode=f"VAX-2602-S{i:02d}", batch=batch2, chamber=chamber2,
                    placed_at=zero,
                )

        self.stdout.write(self.style.SUCCESS("演示数据就绪。"))
        self.stdout.write("账号 / 密码：coord_zhang / analyst_li / qa_wang，密码均为 demo1234")
        from rest_framework.authtoken.models import Token
        for u in User.objects.filter(profile__isnull=False):
            self.stdout.write(f"  {u.username:14s} Token {Token.objects.get(user=u).key}")
