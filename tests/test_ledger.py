import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, LedgerWriteError, PermissionDenied


UW = Actor("uw-1", "underwriter")
CO = Actor("co-1", "claims_officer")
FIN = Actor("fin-1", "finance")
ADMIN = Actor("admin-1", "admin")

TREATY = {"treaty_ref": "TR-1", "event_id": "CAT-2026-01", "attachment": 100.0, "limit": 1100.0, "cession_pct": 0.5, "reinstatement_pct": 0.1}
# 容量 = (1100-100)*0.5 = 500；单笔核定500 -> 摊回 (500-100)*0.5 = 200

LEGACY_DATA = {"event_id": "CAT-2026-01", "attachment": 1000000.0, "limit": 5000000.0, "cession_pct": 0.4, "loss_amount": 3000000.0, "reinstatement_pct": 0.15, "aggregate_prior": 0.0}
LEGACY_FLOW = [
    ("bind", "underwriter", {"underwriter_id": "UW-8"}),
    ("submit_claim", "claims_officer", {"claim_number": "CLM-88", "event_id": "CAT-2026-01"}),
    ("calculate", "claims_officer", {"approved_loss": 2800000.0}),
    ("settle", "finance", {"payment_reference": "PAY-1"}),
]


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)
        self.treaty = self.service.create_treaty(UW, TREATY)

    def tearDown(self):
        self.temp.cleanup()

    def _submit(self, claim_ref, loss=500.0, key=None):
        data = {"claim_ref": claim_ref, "reported_loss": loss}
        if key:
            data["idempotency_key"] = key
        return self.service.submit_claim(CO, self.treaty["id"], data)

    def _approve(self, claim_id, loss=500.0, key=None):
        return self.service.append_fact(CO, claim_id, "approved", {"approved_loss": loss}, key)

    def test_capacity_allocation_and_waiting(self):
        a = self._submit("CLM-A")
        b = self._submit("CLM-B")
        c = self._submit("CLM-C")
        self.assertEqual(self._approve(a["id"])["status"], "allocated")
        self.assertEqual(self._approve(b["id"])["status"], "allocated")
        # 400 + 200 > 500，容量不足进入等待
        self.assertEqual(self._approve(c["id"])["status"], "waiting")
        detail = self.service.get_treaty_detail(CO, self.treaty["id"])
        self.assertEqual(detail["allocated_total"], 400.0)
        self.assertEqual(detail["available_capacity"], 100.0)
        self.assertEqual(detail["waiting_count"], 1)
        self.assertEqual(detail["pending_amount"], 200.0)
        self.assertEqual([item["claim_ref"] for item in detail["waiting_queue"]], ["CLM-C"])
        self.assertIn({"type": "unmet_demand", "amount": 200.0, "waiting_count": 1}, detail["discrepancies"])

    def test_withdraw_settled_refills_waiting(self):
        a = self._submit("CLM-A")
        b = self._submit("CLM-B")
        c = self._submit("CLM-C")
        self._approve(a["id"])
        self._approve(b["id"])
        self._approve(c["id"])
        settled = self.service.append_fact(FIN, a["id"], "settled", {"payment_reference": "PAY-1"})
        self.assertEqual(settled["status"], "settled")
        # 结算后撤回，释放占用，等待中的CLM-C按剩余额度补入
        self.service.append_fact(FIN, a["id"], "withdrawn", {"reason": "分包人退回"})
        detail = self.service.get_treaty_detail(CO, self.treaty["id"])
        by_ref = {claim["claim_ref"]: claim for claim in detail["claims"]}
        self.assertEqual(by_ref["CLM-A"]["status"], "withdrawn")
        self.assertEqual(by_ref["CLM-A"]["allocated"], 0.0)
        self.assertEqual(by_ref["CLM-C"]["status"], "allocated")
        self.assertEqual(by_ref["CLM-C"]["allocated"], 200.0)
        self.assertEqual(detail["waiting_count"], 0)
        self.assertEqual(detail["available_capacity"], 100.0)
        self.assertEqual(detail["premium_due"], 40.0)

    def test_settle_waiting_claim_rejected(self):
        a = self._submit("CLM-A")
        b = self._submit("CLM-B")
        c = self._submit("CLM-C")
        self._approve(a["id"])
        self._approve(b["id"])
        self._approve(c["id"])
        with self.assertRaises(Conflict):
            self.service.append_fact(FIN, c["id"], "settled", {"payment_reference": "PAY-9"})

    def test_duplicate_submission_occupies_once(self):
        first = self._submit("CLM-A", key="KEY-1")
        self.assertFalse(first["duplicate"])
        second = self._submit("CLM-A", key="KEY-1")
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["id"], second["id"])
        facts = self.service.treaty_facts(CO, self.treaty["id"])
        self.assertEqual(len(facts), 1)
        # 重复核定：同幂等键返回原结果，不同键报冲突，均不再占用
        retry = self._approve(first["id"], key="AP-1")
        self.assertFalse(retry["duplicate"])
        self.assertEqual(retry["allocated"], 200.0)
        retry2 = self._approve(first["id"], key="AP-1")
        self.assertTrue(retry2["duplicate"])
        with self.assertRaises(Conflict):
            self._approve(first["id"])
        detail = self.service.get_treaty_detail(CO, self.treaty["id"])
        self.assertEqual(detail["allocated_total"], 200.0)

    def test_concurrent_submission_single_occupancy(self):
        barrier = threading.Barrier(8)
        results = []
        errors = []

        def submit():
            try:
                barrier.wait(timeout=10)
                results.append(self._submit("CLM-CONC"))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(len({item["id"] for item in results}), 1)
        self.assertEqual(sum(1 for item in results if not item["duplicate"]), 1)
        detail = self.service.get_treaty_detail(CO, self.treaty["id"])
        self.assertEqual(len(detail["claims"]), 1)
        claim_id = detail["claims"][0]["id"]
        self._approve(claim_id)
        detail = self.service.get_treaty_detail(CO, self.treaty["id"])
        self.assertEqual(detail["allocated_total"], 200.0)
        self.assertEqual(detail["available_capacity"], 300.0)

    def test_write_failure_resumes_from_checkpoint(self):
        original = self.service.repository.replay_and_persist
        calls = {"n": 0}

        def flaky(treaty_id, replay_fn):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("模拟写入失败")
            return original(treaty_id, replay_fn)

        self.service.repository.replay_and_persist = flaky
        try:
            with self.assertRaises(LedgerWriteError):
                self._submit("CLM-F", key="RETRY-1")
        finally:
            self.service.repository.replay_and_persist = original
        # 事实已追加但投影未落库；重试同一请求从最后完整账本继续，不重复占用
        retry = self._submit("CLM-F", key="RETRY-1")
        self.assertTrue(retry["duplicate"])
        detail = self.service.get_treaty_detail(CO, self.treaty["id"])
        self.assertEqual(len(detail["claims"]), 1)
        self.assertEqual(detail["checkpoint"]["last_fact_id"], 1)
        self._approve(retry["id"])
        detail = self.service.get_treaty_detail(CO, self.treaty["id"])
        self.assertEqual(detail["allocated_total"], 200.0)
        self.assertEqual(detail["available_capacity"], 300.0)

    def test_backfill_from_audit_timeline(self):
        record = self.service.create(UW, "RI-25001", LEGACY_DATA)
        for action, role, data in LEGACY_FLOW:
            record = self.service.act(Actor("op", role), record["id"], record["version"], action, data)
        report = self.service.backfill_legacy(ADMIN)
        self.assertEqual(report["backfilled"], 1)
        item = report["items"][0]
        self.assertEqual(item["status"], "backfilled")
        detail = self.service.get_treaty_detail(ADMIN, item["treaty_id"])
        self.assertEqual(detail["treaty_ref"], "LEGACY-RI-25001")
        self.assertEqual(detail["capacity"], 1600000.0)
        claim = detail["claims"][0]
        self.assertEqual(claim["claim_ref"], "CLM-88")
        self.assertEqual(claim["status"], "settled")
        self.assertEqual(claim["version"], 3)
        self.assertEqual(claim["allocated"], 720000.0)
        self.assertEqual(claim["reinstatement_premium"], 108000.0)
        self.assertEqual(detail["available_capacity"], 880000.0)
        # 重复回填不会重复建案
        again = self.service.backfill_legacy(ADMIN)
        self.assertEqual(again["skipped"], 1)

    def test_backfill_missing_timeline_stops_at_pending_review(self):
        self.service.repository.create("RI-90002", "settled", dict(LEGACY_DATA), "legacy")
        report = self.service.backfill_legacy(ADMIN)
        self.assertEqual(report["pending_review"], 1)
        item = report["items"][0]
        self.assertEqual(item["status"], "pending_review")
        detail = self.service.get_treaty_detail(ADMIN, item["treaty_id"])
        claim = detail["claims"][0]
        self.assertEqual(claim["status"], "pending_review")
        self.assertTrue(claim["review_pending"])
        self.assertEqual(detail["allocated_total"], 0.0)
        self.assertEqual(detail["available_capacity"], detail["capacity"])
        self.assertEqual(detail["pending_review_count"], 1)
        self.assertTrue(any(d["type"] == "pending_review" for d in detail["discrepancies"]))

    def test_backfill_version_gap_stops_at_pending_review(self):
        record = self.service.create(UW, "RI-90003", LEGACY_DATA)
        record = self.service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-1"})
        record = self.service.act(CO, record["id"], record["version"], "submit_claim", {"claim_number": "CLM-9", "event_id": "CAT-2026-01"})
        connection = sqlite3.connect(self.db_path)
        connection.execute("DELETE FROM audit_events WHERE record_id=? AND action='bind'", (record["id"],))
        connection.commit()
        connection.close()
        report = self.service.backfill_legacy(ADMIN)
        self.assertEqual(report["pending_review"], 1)
        self.assertIn("版本缺口", report["items"][0]["reason"])

    def test_stats_and_list_expose_capacity_waiting_discrepancy(self):
        a = self._submit("CLM-A")
        b = self._submit("CLM-B")
        c = self._submit("CLM-C")
        self._approve(a["id"])
        self._approve(b["id"])
        self._approve(c["id"])
        self.service.repository.create("RI-90004", "settled", dict(LEGACY_DATA), "legacy")
        self.service.backfill_legacy(ADMIN)
        summaries = self.service.list_treaties(CO)
        self.assertEqual(len(summaries), 2)
        main = next(item for item in summaries if item["treaty_ref"] == "TR-1")
        self.assertEqual(main["available_capacity"], 100.0)
        self.assertEqual(main["waiting_count"], 1)
        stats = self.service.stats(CO)
        self.assertEqual(stats["ledger"]["waiting_count"], 1)
        self.assertEqual(stats["ledger"]["pending_amount"], 200.0)
        self.assertEqual(stats["ledger"]["pending_review_count"], 1)
        types = {item["type"] for item in stats["ledger"]["discrepancies"]}
        self.assertEqual(types, {"unmet_demand", "pending_review"})
        self.assertIn("states", stats)

    def test_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_treaty(CO, {"treaty_ref": "TR-2", "event_id": "E", "attachment": 0.0, "limit": 10.0, "cession_pct": 1.0, "reinstatement_pct": 0.0})
        with self.assertRaises(PermissionDenied):
            self.service.submit_claim(UW, self.treaty["id"], {"claim_ref": "CLM-X", "reported_loss": 1.0})
        claim = self._submit("CLM-X")
        with self.assertRaises(PermissionDenied):
            self.service.append_fact(FIN, claim["id"], "approved", {"approved_loss": 1.0})
        with self.assertRaises(PermissionDenied):
            self.service.backfill_legacy(CO)
