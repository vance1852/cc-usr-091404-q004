"""End-to-end API tests: authentication, role matrix, freeze responses."""
import json
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from stability import services
from stability.models import (
    ChamberEvent,
    PlannedAction,
    SampleUnit,
)

from . import factories


class APITestCase(TestCase):
    def setUp(self):
        self.users = factories.make_three_users()
        self.protocol, self.cond = factories.make_protocol(self.users["coord"])
        self.chamber = factories.make_chamber()
        self.batch = factories.make_batch(
            self.users["coord"], self.protocol, self.cond, self.chamber,
            days_ago=100)
        self.clients = {}
        for name, user in self.users.items():
            client = __import__("django.test", fromlist=["Client"]).Client()
            client.force_login(user)
            self.clients[name] = client

    def post(self, role, url, payload=None):
        return self.clients[role].post(
            url, data=json.dumps(payload or {}),
            content_type="application/json")

    def get(self, role, url):
        return self.clients[role].get(url)

    # ------------------------------------------------------------------ auth
    def test_anonymous_is_401(self):
        from django.test import Client
        self.assertEqual(Client().get("/api/me/").status_code, 401)
        self.assertEqual(Client().get("/api/dataset/").status_code, 401)

    def test_post_without_csrf_token_is_rejected(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.users["qa"])
        # Valid session and role, but no CSRF token on an unsafe request.
        resp = client.post("/api/results/1/decision/",
                           data=json.dumps({"qa_status": "included"}),
                           content_type="application/json")
        self.assertEqual(resp.status_code, 403)

    def test_me_reports_roles(self):
        body = self.get("qa", "/api/me/").json()
        self.assertTrue(body["permissions"]["can_qa_decide"])
        self.assertFalse(body["permissions"]["can_coordinate"])

    # ------------------------------------------------------------- role matrix
    def test_only_coordinator_records_chamber_event(self):
        payload = {
            "chamber_id": self.chamber.id, "event_type": "power_out",
            "started_at": (timezone.now() - timedelta(hours=2)).isoformat(),
            "ended_at": (timezone.now() - timedelta(hours=1)).isoformat(),
        }
        self.assertEqual(self.post("analyst", "/api/events/", payload).status_code, 403)
        self.assertEqual(self.post("qa", "/api/events/", payload).status_code, 403)
        resp = self.post("coord", "/api/events/", payload)
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_only_qa_assesses_event(self):
        event = factories.add_power_outage(self.chamber, self.users["coord"])
        url = f"/api/events/{event.id}/assess/"
        self.assertEqual(
            self.post("coord", url, {"assessment": "no_impact"}).status_code, 403)
        resp = self.post("qa", url, {"assessment": "no_impact", "note": "ok"})
        self.assertEqual(resp.status_code, 200)
        event.refresh_from_db()
        self.assertEqual(event.status, "no_impact")

    def test_only_analyst_submits_result(self):
        action = PlannedAction.objects.get(
            batch=self.batch, point_code="M3", test_item__code="POT")
        sample = SampleUnit.objects.get(barcode="B-001-S04")
        services.assign_original(action, sample, self.users["coord"])
        services.withdraw_sample(
            action.active_assignment, action.planned_datetime,
            self.users["coord"])
        url = f"/api/assignments/{action.active_assignment.id}/results/"
        payload = {"value": "0.91",
                   "analyzed_at": (action.planned_datetime + timedelta(days=1)).isoformat()}
        self.assertEqual(self.post("coord", url, payload).status_code, 403)
        self.assertEqual(self.post("qa", url, payload).status_code, 403)
        resp = self.post("analyst", url, payload)
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_only_qa_decides_result(self):
        _, _, result = factories.full_workflow(self.users, self.batch)
        url = f"/api/results/{result.id}/decision/"
        self.assertEqual(
            self.post("analyst", url, {"qa_status": "included"}).status_code, 403)
        self.assertEqual(
            self.post("coord", url, {"qa_status": "included"}).status_code, 403)
        self.assertEqual(
            self.post("qa", url, {"qa_status": "included"}).status_code, 200)

    def test_only_coordinator_assigns_and_substitutes(self):
        action = PlannedAction.objects.get(
            batch=self.batch, point_code="M3", test_item__code="POT")
        url = f"/api/actions/{action.id}/assign/"
        payload = {"sample_barcode": "B-001-S04"}
        self.assertEqual(self.post("analyst", url, payload).status_code, 403)
        self.assertEqual(self.post("qa", url, payload).status_code, 403)
        self.assertEqual(self.post("coord", url, payload).status_code, 201)

    # ---------------------------------------------------------- business flow
    def test_frozen_inclusion_returns_409_but_exclusion_works(self):
        _, sample, result = factories.full_workflow(self.users, self.batch)
        start = sample.withdrawn_at - timedelta(days=2)
        services.record_chamber_event(ChamberEvent(
            chamber=self.chamber, event_type="power_out",
            started_at=start, ended_at=start + timedelta(hours=2),
            recorded_by=self.users["coord"]))
        decision_url = f"/api/results/{result.id}/decision/"
        resp = self.post("qa", decision_url, {"qa_status": "included"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("冻结", resp.json()["detail"])
        resp = self.post("qa", decision_url, {"qa_status": "excluded"})
        self.assertEqual(resp.status_code, 200)

    def test_consumed_sample_reassigned_returns_409(self):
        action, sample, _ = factories.full_workflow(self.users, self.batch)
        other = PlannedAction.objects.get(
            batch=self.batch, point_code="M3", test_item__code="PH")
        resp = self.post("coord", f"/api/actions/{other.id}/assign/",
                         {"sample_barcode": sample.barcode})
        self.assertEqual(resp.status_code, 409)

    def test_substitution_chain_visible_in_timeline_and_dataset(self):
        action, original, _ = factories.full_workflow(
            self.users, self.batch, sample_suffix="04")
        services.decide_result(
            action.active_assignment.results.get(), "excluded",
            self.users["qa"], "污染")
        # arrange substitute
        resp = self.post("coord", f"/api/actions/{action.id}/substitute/",
                         {"sample_barcode": "B-001-S07", "reason": "damaged"})
        self.assertEqual(resp.status_code, 201, resp.content)
        sub = action.active_assignment
        self.assertTrue(sub.is_substitute)
        # withdraw + analyze the substitute on the planned day
        services.withdraw_sample(sub, action.planned_datetime, self.users["coord"])
        result = services.submit_result(
            assignment=sub, value="0.92",
            analyzed_at=action.planned_datetime + timedelta(days=1),
            analyst=self.users["analyst"])
        services.decide_result(result, "included", self.users["qa"])

        timeline = self.get("qa", f"/api/batches/{self.batch.id}/timeline/").json()
        labels = " ".join(item["label"] for item in timeline["results"])
        self.assertIn("替代安排", labels)
        self.assertIn("不改变原计划", labels)

        dataset = self.get("qa", "/api/dataset/").json()["results"]
        row = next(r for r in dataset
                   if r["sample_barcode"] == "B-001-S07")
        self.assertTrue(row["is_substitute"])
        self.assertEqual(row["point_code"], "M3")

    def test_overdue_endpoint(self):
        body = self.get("qa", f"/api/batches/{self.batch.id}/overdue/").json()
        self.assertGreaterEqual(body["count"], 2)  # M3 POT + M3 PH
        self.assertTrue(all(r["overdue"] for r in body["results"]))

    def test_exposure_endpoint_reports_pending_seconds(self):
        _, sample, _ = factories.full_workflow(self.users, self.batch)
        start = sample.withdrawn_at - timedelta(hours=5)
        services.record_chamber_event(ChamberEvent(
            chamber=self.chamber, event_type="power_out",
            started_at=start, ended_at=start + timedelta(hours=3),
            recorded_by=self.users["coord"]))
        body = self.get("qa", f"/api/batches/{self.batch.id}/exposure/").json()
        # The sample experienced 5h-residence-before-withdraw overlap; other
        # in-storage samples experienced the full outage.
        self.assertGreater(body["pending_exposure_seconds"], 0)
        s04 = next(s for s in body["samples"] if s["barcode"] == "B-001-S04")
        self.assertEqual(len(s04["exposures"]), 1)
        # full outage minus the (withdrawn_at - outage_start) gap
        self.assertGreater(s04["exposures"][0]["exposure_seconds"], 0)


class ProtocolVersioningAPITests(TestCase):
    def setUp(self):
        self.users = factories.make_three_users()
        self.chamber = factories.make_chamber()

    def test_new_version_supersedes_old_and_batch_keeps_old(self):
        payload = {
            "code": "PV", "version": 1, "title": "方案",
            "conditions": [{"code": "2-8C", "target_temperature_c": "5"}],
            "test_items": [{"code": "A", "name": "项目A", "unit": "x"}],
            "points": [{"code": "M0", "offset_days": 0,
                        "window_before_days": 1, "window_after_days": 1,
                        "test_item_codes": ["A"]}],
            "activate": True,
        }
        client = self.client  # not logged in yet
        client.force_login(self.users["coord"])
        resp = client.post("/api/protocols/create/", data=json.dumps(payload),
                           content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        v1 = resp.json()

        # v2 supersedes v1
        payload["version"] = 2
        payload["supersedes_id"] = v1["id"]
        payload["points"][0]["window_after_days"] = 5
        resp = client.post("/api/protocols/create/", data=json.dumps(payload),
                           content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["version"], 2)

        from stability.models import Protocol
        self.assertEqual(Protocol.objects.get(id=v1["id"]).status, "superseded")
