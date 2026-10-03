import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied
from src.ledger import expected_facts_from_timeline, replay

UW = Actor("uw-1", "underwriter")
CO = Actor("co-1", "claims_officer")
FIN = Actor("fin-1", "finance")
ADMIN = Actor("boss", "admin")
EVENT = "CAT-2026-01"


def contract_data(event_id=EVENT, loss=6000000.0, prior=0.0):
    return {'event_id': event_id, 'attachment': 1000000.0, 'limit': 5000000.0, 'cession_pct': 0.4, 'loss_amount': loss, 'reinstatement_pct': 0.15, 'aggregate_prior': prior}


class LedgerBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def make_record(self, reference, event_id=EVENT, loss=6000000.0, prior=0.0):
        return self.service.create(UW, reference, contract_data(event_id, loss, prior))

    def run_flow(self, record, claim_number, approved_loss, event_id=EVENT, settle=False):
        record = self.service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-1"})
        record = self.service.act(CO, record["id"], record["version"], "submit_claim", {"claim_number": claim_number, "event_id": event_id})
        record = self.service.act(CO, record["id"], record["version"], "calculate", {"approved_loss": approved_loss})
        if settle:
            record = self.service.act(FIN, record["id"], record["version"], "settle", {"payment_reference": "PAY-" + claim_number})
        return record

    def snapshot(self, event_id=EVENT):
        return self.service.event_ledger(ADMIN, event_id)["snapshot"]

    def wipe_ledger(self):
        con = sqlite3.connect(self.db_path)
        con.execute("DELETE FROM ledger_facts")
        con.execute("DELETE FROM ledger_snapshots")
        con.commit()
        con.close()


class WaitingQueueTest(LedgerBase):
    def _three_contracts(self):
        # 每层容量 (5M-1M)*0.4 = 1.6M；第三层既往累计已耗 1.6M，有效容量为 0。
        r1 = self.run_flow(self.make_record("RI-1"), "CLM-1", 6000000.0)   # 摊回 1.6M
        r2 = self.run_flow(self.make_record("RI-2"), "CLM-2", 6000000.0)   # 摊回 1.6M
        r3 = self.run_flow(self.make_record("RI-3", prior=1600000.0, loss=2000000.0), "CLM-3", 2000000.0)  # 摊回 0.4M
        return r1, r2, r3

    def test_waiting_queue_and_withdraw_refill(self):
        r1, r2, r3 = self._three_contracts()
        snap = self.snapshot()
        self.assertEqual(snap["capacity_total"], 4800000.0)
        self.assertEqual(snap["aggregate_prior_total"], 1600000.0)
        self.assertEqual(snap["allocated_total"], 3200000.0)
        self.assertEqual(snap["available_capacity"], 0.0)
        self.assertEqual(snap["waiting"], [str(r3["id"])])
        self.assertEqual(snap["claims"][str(r3["id"])]["status"], "waiting")
        self.assertEqual(snap["claims"][str(r3["id"])]["waiting_position"], 1)
        self.assertEqual(snap["waiting_amount"], 400000.0)
        # 已结算赔案撤回后释放额度，等待中的赔案按剩余额度补入。
        r1 = self.service.act(FIN, r1["id"], r1["version"], "settle", {"payment_reference": "PAY-1"})
        r1 = self.service.act(FIN, r1["id"], r1["version"], "withdraw", {"reason": "分保人撤回"})
        self.assertEqual(r1["state"], "withdrawn")
        snap = self.snapshot()
        self.assertEqual(snap["waiting"], [])
        self.assertEqual(snap["claims"][str(r3["id"])]["status"], "allocated")
        self.assertEqual(snap["claims"][str(r3["id"])]["allocated"], 400000.0)
        self.assertEqual(snap["available_capacity"], 1200000.0)
        # 恢复保费随重算对齐：CLM-2 1.6M*0.15 + CLM-3 0.4M*0.15。
        self.assertEqual(snap["reinstatement_premium_total"], 300000.0)
        self.assertEqual(snap["pending_amount"], 2000000.0)

    def test_settle_blocked_while_waiting(self):
        _, _, r3 = self._three_contracts()
        with self.assertRaises(Conflict):
            self.service.act(FIN, r3["id"], r3["version"], "settle", {"payment_reference": "PAY-3"})

    def test_withdraw_roles_and_transitions(self):
        record = self.make_record("RI-9")
        with self.assertRaises(Conflict):
            self.service.act(FIN, record["id"], record["version"], "withdraw", {"reason": "太早"})
        record = self.service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-1"})
        with self.assertRaises(PermissionDenied):
            self.service.act(UW, record["id"], record["version"], "withdraw", {"reason": "越权"})
        record = self.service.act(CO, record["id"], record["version"], "submit_claim", {"claim_number": "CLM-9", "event_id": EVENT})
        record = self.service.act(CO, record["id"], record["version"], "withdraw", {"reason": "撤案"})
        self.assertEqual(record["state"], "withdrawn")


class IdempotencyTest(LedgerBase):
    def test_duplicate_claim_number_conflict(self):
        r1 = self.run_flow_no_calc(self.make_record("RI-1"), "CLM-X")
        r2 = self.run_flow_no_calc(self.make_record("RI-2"), "CLM-Y")
        with self.assertRaises(Conflict):
            self.service.act(CO, r2["id"], r2["version"], "submit_claim", {"claim_number": "CLM-X", "event_id": EVENT})
        # 重复提交被拒绝后账本不含第二笔占用。
        snap = self.snapshot()
        submitted = [c for c in snap["claims"].values() if c["claim_number"] == "CLM-X"]
        self.assertEqual(len(submitted), 1)

    def run_flow_no_calc(self, record, claim_number):
        record = self.service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-1"})
        record = self.service.act(CO, record["id"], record["version"], "submit_claim", {"claim_number": claim_number, "event_id": EVENT})
        # 撤回后赔案号可复用
        return record

    def test_claim_number_reusable_after_withdraw(self):
        r1 = self.run_flow_no_calc(self.make_record("RI-1"), "CLM-X")
        r1 = self.service.act(CO, r1["id"], r1["version"], "withdraw", {"reason": "撤案"})
        r2 = self.run_flow_no_calc(self.make_record("RI-2"), "CLM-X")
        self.assertEqual(r2["state"], "claim_submitted")

    def test_failed_write_resumes_from_last_ledger(self):
        record = self.make_record("RI-1")
        record = self.service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-1"})
        record = self.service.act(CO, record["id"], record["version"], "submit_claim", {"claim_number": "CLM-1", "event_id": EVENT})
        repo = self.service.repository
        original = repo._append_fact_tx
        state = {"failed": False}

        def flaky(*args, **kwargs):
            if not state["failed"]:
                state["failed"] = True
                raise sqlite3.OperationalError("disk I/O error")
            return original(*args, **kwargs)

        repo._append_fact_tx = flaky
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.service.act(CO, record["id"], record["version"], "calculate", {"approved_loss": 2800000.0})
        finally:
            repo._append_fact_tx = original
        # 失败回滚：记录版本与账本都停留在最后完整状态。
        fresh = self.service.get_record(ADMIN, record["id"])
        self.assertEqual(fresh["version"], record["version"])
        self.assertEqual(fresh["state"], "claim_submitted")
        self.assertEqual(self.snapshot()["seq"], 2)
        # 用同一版本号重试成功，事实只追加一次。
        record = self.service.act(CO, record["id"], record["version"], "calculate", {"approved_loss": 2800000.0})
        self.assertEqual(record["state"], "calculated")
        facts = self.service.event_ledger(ADMIN, EVENT)["facts"]
        self.assertEqual([f["seq"] for f in facts], [1, 2, 3])
        assessed = [f for f in facts if f["fact_type"] == "claim_assessed"]
        self.assertEqual(len(assessed), 1)
        self.assertEqual(self.snapshot()["claims"][str(record["id"])]["allocated"], 720000.0)


class ConcurrencyTest(LedgerBase):
    def test_concurrent_submissions_allocate_once(self):
        errors = []

        def worker(index):
            try:
                record = self.service.create(UW, "RI-C-%d" % index, contract_data(loss=2000000.0))
                record = self.service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-%d" % index})
                record = self.service.act(CO, record["id"], record["version"], "submit_claim", {"claim_number": "CLM-C-%d" % index, "event_id": EVENT})
                self.service.act(CO, record["id"], record["version"], "calculate", {"approved_loss": 2000000.0})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        facts = self.service.event_ledger(ADMIN, EVENT)["facts"]
        self.assertEqual(len(facts), 24)
        self.assertEqual(sorted(f["seq"] for f in facts), list(range(1, 25)))
        snap = self.snapshot()
        self.assertEqual(snap["seq"], 24)
        self.assertEqual(len(snap["claims"]), 8)
        self.assertEqual(snap["allocated_total"], 3200000.0)
        self.assertEqual(snap["available_capacity"], 9600000.0)
        for claim in snap["claims"].values():
            self.assertEqual(claim["status"], "allocated")
            self.assertEqual(claim["allocated"], 400000.0)


class BackfillTest(LedgerBase):
    def test_backfill_from_audit_timeline(self):
        r1 = self.run_flow(self.make_record("RI-1"), "CLM-1", 2800000.0, settle=True)
        r2 = self.make_record("RI-2")
        r2 = self.service.act(UW, r2["id"], r2["version"], "bind", {"underwriter_id": "UW-2"})
        before = self.snapshot()
        self.wipe_ledger()
        report = self.service.backfill(ADMIN)
        self.assertEqual(sorted(report["backfilled"]), [r1["id"], r2["id"]])
        self.assertEqual(report["pending_review"], [])
        after = self.snapshot()
        self.assertEqual(after, before)
        claim = after["claims"][str(r1["id"])]
        self.assertEqual(claim["status"], "settled")
        self.assertEqual(claim["claim_version"], 5)  # 赔案版本按审计时间线回填
        # 回填幂等：再次执行全部跳过。
        report = self.service.backfill(ADMIN)
        self.assertEqual(report["backfilled"], [])
        self.assertEqual(sorted(report["skipped"]), [r1["id"], r2["id"]])

    def test_backfill_pending_review_on_broken_timeline(self):
        r1 = self.run_flow(self.make_record("RI-1"), "CLM-1", 2800000.0)
        con = sqlite3.connect(self.db_path)
        con.execute("DELETE FROM audit_events WHERE record_id=? AND action='calculate'", (r1["id"],))
        con.commit()
        con.close()
        self.wipe_ledger()
        report = self.service.backfill(ADMIN)
        self.assertEqual(report["backfilled"], [])
        self.assertEqual([item["record_id"] for item in report["pending_review"]], [r1["id"]])
        snap = self.snapshot()
        claim = snap["claims"][str(r1["id"])]
        self.assertEqual(claim["status"], "pending_review")
        self.assertEqual(claim["allocated"], 0.0)
        self.assertEqual(snap["pending_review"], [str(r1["id"])])
        detail = self.service.get_record(ADMIN, r1["id"])
        self.assertEqual(detail["discrepancies"][0]["type"], "pending_review")
        stats = self.service.stats(ADMIN)
        self.assertEqual(stats["ledger"]["pending_review_claims"], 1)
        self.assertEqual(stats["ledger"]["discrepancies"], 1)

    def test_backfill_fills_missing_contract_fact(self):
        # 旧数据在账本启用前已绑定并提交赔案，晚到核定后才回填合约容量。
        record = self.make_record("RI-1")
        record = self.service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-1"})
        record = self.service.act(CO, record["id"], record["version"], "submit_claim", {"claim_number": "CLM-1", "event_id": EVENT})
        self.wipe_ledger()
        record = self.service.act(CO, record["id"], record["version"], "calculate", {"approved_loss": 2800000.0})
        self.assertEqual(self.snapshot()["claims"][str(record["id"])]["status"], "waiting")
        report = self.service.backfill(ADMIN)
        self.assertEqual(report["backfilled"], [record["id"]])
        snap = self.snapshot()
        self.assertEqual(snap["capacity_total"], 1600000.0)
        self.assertEqual(snap["claims"][str(record["id"])]["status"], "allocated")
        self.assertEqual(snap["claims"][str(record["id"])]["allocated"], 720000.0)

    def test_backfill_requires_admin(self):
        with self.assertRaises(PermissionDenied):
            self.service.backfill(CO)


class ViewTest(LedgerBase):
    def test_list_detail_stats_expose_ledger(self):
        r1 = self.run_flow(self.make_record("RI-1"), "CLM-1", 6000000.0)
        r2 = self.run_flow(self.make_record("RI-2", prior=1600000.0, loss=2000000.0), "CLM-2", 2000000.0)
        listing = self.service.list_records(ADMIN)
        self.assertIn("events", listing)
        summary = listing["events"][EVENT]
        self.assertEqual(summary["available_capacity"], 0.0)
        self.assertEqual(summary["waiting"], ["CLM-2"])
        self.assertEqual(summary["waiting_count"], 1)
        by_id = {item["id"]: item for item in listing["items"]}
        self.assertEqual(by_id[r1["id"]]["allocation"]["status"], "allocated")
        self.assertEqual(by_id[r2["id"]]["allocation"]["status"], "waiting")
        self.assertEqual(by_id[r2["id"]]["allocation"]["waiting_position"], 1)
        detail = self.service.get_record(ADMIN, r2["id"])
        self.assertEqual(detail["event_ledger"]["available_capacity"], 0.0)
        self.assertEqual(detail["event_ledger"]["waiting"], ["CLM-2"])
        self.assertEqual(detail["discrepancies"], [])
        stats = self.service.stats(ADMIN)
        self.assertEqual(stats["ledger"]["events"], 1)
        self.assertEqual(stats["ledger"]["waiting_claims"], 1)
        self.assertEqual(stats["ledger"]["available_capacity"], 0.0)
        self.assertEqual(stats["ledger"]["discrepancies"], 0)
        self.assertEqual(stats["states"]["calculated"], 2)
        ledger_view = self.service.event_ledger(ADMIN, EVENT)
        self.assertEqual(ledger_view["summary"]["waiting"], ["CLM-2"])
        self.assertEqual(len(ledger_view["facts"]), 6)


class ReplayTest(unittest.TestCase):
    def test_replay_duplicate_submission_ignored(self):
        facts = [
            {"seq": 1, "fact_type": "contract_bound", "fact": {"record_id": 1, "capacity": 100.0, "aggregate_prior": 0.0}, "submission_key": None},
            {"seq": 2, "fact_type": "claim_submitted", "fact": {"record_id": 1, "claim_number": "C1"}, "submission_key": "k1"},
            {"seq": 3, "fact_type": "claim_submitted", "fact": {"record_id": 1, "claim_number": "C1"}, "submission_key": "k1"},
            {"seq": 4, "fact_type": "claim_assessed", "fact": {"record_id": 1, "recoverable": 60.0, "reinstatement_pct": 0.5, "claim_version": 3}, "submission_key": None},
        ]
        snap = replay(facts)
        self.assertEqual(len(snap["claims"]), 1)
        self.assertEqual(snap["allocated_total"], 60.0)
        self.assertEqual(snap["reinstatement_premium_total"], 30.0)

    def test_replay_waiting_and_withdraw_refill(self):
        facts = [
            {"seq": 1, "fact_type": "contract_bound", "fact": {"record_id": 1, "capacity": 100.0, "aggregate_prior": 0.0}, "submission_key": None},
            {"seq": 2, "fact_type": "claim_assessed", "fact": {"record_id": 1, "recoverable": 80.0, "reinstatement_pct": 0.0, "claim_version": 2}, "submission_key": None},
            {"seq": 3, "fact_type": "claim_assessed", "fact": {"record_id": 2, "recoverable": 50.0, "reinstatement_pct": 0.0, "claim_version": 2}, "submission_key": None},
            {"seq": 4, "fact_type": "claim_settled", "fact": {"record_id": 1, "payment_reference": "P", "claim_version": 3}, "submission_key": None},
        ]
        snap = replay(facts)
        self.assertEqual(snap["waiting"], ["2"])
        self.assertEqual(snap["allocated_total"], 80.0)
        facts.append({"seq": 5, "fact_type": "claim_withdrawn", "fact": {"record_id": 1, "reason": "x", "claim_version": 4}, "submission_key": None})
        snap = replay(facts)
        self.assertEqual(snap["waiting"], [])
        self.assertEqual(snap["claims"]["2"]["allocated"], 50.0)
        self.assertEqual(snap["available_capacity"], 50.0)

    def test_expected_facts_version_gap(self):
        record = {"id": 7, "state": "calculated", "payload": {"event_id": "E", "layer_width": 100.0, "cession_pct": 0.5, "attachment": 0.0, "limit": 100.0, "loss_amount": 10.0, "claim_number": "C", "approved_loss": 10.0, "recoverable_amount": 5.0, "reinstatement_pct": 0.1, "aggregate_prior": 0.0}}
        timeline = [
            {"action": "created", "version": 1},
            {"action": "bind", "version": 2},
            {"action": "calculate", "version": 4},  # 版本断档
        ]
        facts, error = expected_facts_from_timeline(record, timeline)
        self.assertEqual(facts, [])
        self.assertIn("断档", error)

    def test_expected_facts_state_mismatch(self):
        record = {"id": 7, "state": "settled", "payload": {"event_id": "E", "layer_width": 100.0, "cession_pct": 0.5, "attachment": 0.0, "limit": 100.0, "loss_amount": 10.0, "claim_number": "C", "approved_loss": 10.0, "recoverable_amount": 5.0, "reinstatement_pct": 0.1, "aggregate_prior": 0.0}}
        timeline = [
            {"action": "created", "version": 1},
            {"action": "bind", "version": 2},
        ]
        facts, error = expected_facts_from_timeline(record, timeline)
        self.assertEqual(facts, [])
        self.assertIn("不一致", error)
