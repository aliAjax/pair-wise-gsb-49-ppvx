"""业务用例编排、权限检查与审计。"""
import threading
from typing import Any, Dict, List, Optional

from . import ledger as ledger_mod
from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, text
from .ledger import ACTION_FACT_TYPES
from .repository import Repository
from .rules import DomainRules

ADMIN_ROLE = "admin"


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self._event_locks: Dict[str, threading.Lock] = {}
        self._event_locks_guard = threading.Lock()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _ensure_admin(self, actor: Actor) -> None:
        if actor.role != ADMIN_ROLE:
            raise PermissionDenied("仅管理员可执行该操作")

    def _event_lock(self, event_id: str) -> threading.Lock:
        with self._event_locks_guard:
            lock = self._event_locks.get(event_id)
            if lock is None:
                lock = threading.Lock()
                self._event_locks[event_id] = lock
            return lock

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        records = self.repository.list_records(state=state, limit=limit)
        items = []
        events: Dict[str, Any] = {}
        cache: Dict[str, Any] = {}
        for record in records:
            event_id = str(record["payload"].get("event_id", ""))
            if event_id not in cache:
                snapshot = self._event_snapshot(event_id)
                discrepancies = self._discrepancies(self.repository.records_for_event(event_id), snapshot)
                cache[event_id] = (snapshot, discrepancies)
            snapshot, discrepancies = cache[event_id]
            item = dict(record)
            item["allocation"] = self._claim_view(snapshot, record["id"])
            items.append(item)
            events[event_id] = self._event_summary(event_id, snapshot, len(discrepancies))
        return {"items": items, "events": events}

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        event_id = str(record["payload"].get("event_id", ""))
        snapshot = self._event_snapshot(event_id)
        discrepancies = self._discrepancies(self.repository.records_for_event(event_id), snapshot)
        result = dict(record)
        result["allocation"] = self._claim_view(snapshot, record_id)
        result["event_ledger"] = self._event_summary(event_id, snapshot, len(discrepancies))
        result["discrepancies"] = discrepancies
        return result

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        if action in ACTION_FACT_TYPES:
            # 同一巨灾事件的记账动作串行执行：并发提交只能占用一次容量。
            event_id = str(record["payload"].get("event_id", ""))
            with self._event_lock(event_id):
                record = self.repository.get(record_id)
                return self._apply(record, expected_version, action, data, actor)
        return self._apply(record, expected_version, action, data, actor)

    def _apply(self, record: Dict[str, Any], expected_version: int, action: str, data: Dict[str, Any], actor: Actor) -> Dict[str, Any]:
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        ledger = None
        if action in ACTION_FACT_TYPES:
            if action == "submit_claim":
                self._ensure_claim_unique(record, new_payload)
            if action == "settle":
                self._ensure_claim_allocated(record)
            built = ledger_mod.fact_for_action(action, record, new_payload, int(expected_version) + 1)
            if built is not None:
                fact_type, fact, submission_key = built
                ledger = {"event_id": str(record["payload"].get("event_id", "")), "fact_type": fact_type, "fact": fact, "submission_key": submission_key}
        return self.repository.mutate(
            record_id=record["id"],
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
            ledger=ledger,
        )

    def _ensure_claim_unique(self, record: Dict[str, Any], new_payload: Dict[str, Any]) -> None:
        """同一巨灾事件下赔案号唯一（已关闭的赔案除外），重复提交不再占用。"""
        event_id = str(record["payload"].get("event_id", ""))
        claim_number = str(new_payload.get("claim_number", ""))
        for other in self.repository.records_for_event(event_id):
            if other["id"] == record["id"] or other["state"] in {"rejected", "withdrawn"}:
                continue
            if str(other["payload"].get("claim_number", "")) == claim_number:
                raise Conflict("赔案号在该巨灾事件下已存在，重复提交不再占用容量")

    def _ensure_claim_allocated(self, record: Dict[str, Any]) -> None:
        """结算前确认赔案已分到容量；等待队列中的赔案不能结算。"""
        event_id = str(record["payload"].get("event_id", ""))
        snapshot = self._event_snapshot(event_id)
        claim = snapshot["claims"].get(str(record["id"]))
        if claim is None:
            raise Conflict("赔案未入账，请先执行回填或重算")
        if claim["status"] == "waiting":
            raise Conflict("赔案在等待队列中，剩余容量不足，暂不能结算")
        if claim["status"] == "pending_review":
            raise Conflict("赔案待核对，暂不能结算")

    def _event_snapshot(self, event_id: str) -> Dict[str, Any]:
        """读取事件账本快照；快照缺失或落后于事实流时自动从事实重建。"""
        snapshot = self.repository.snapshot_for_event(event_id)
        if snapshot is None:
            if self.repository.max_fact_seq(event_id) > 0:
                return self.repository.rebuild_snapshot(event_id)
            return ledger_mod.empty_snapshot()
        if int(snapshot["seq"]) != self.repository.max_fact_seq(event_id):
            return self.repository.rebuild_snapshot(event_id)
        return snapshot

    @staticmethod
    def _claim_view(snapshot: Dict[str, Any], record_id: int) -> Optional[Dict[str, Any]]:
        claim = snapshot["claims"].get(str(record_id))
        return dict(claim) if claim is not None else None

    @staticmethod
    def _event_summary(event_id: str, snapshot: Dict[str, Any], discrepancy_count: int) -> Dict[str, Any]:
        claims = snapshot["claims"]
        return {
            "event_id": event_id,
            "capacity_total": snapshot["capacity_total"],
            "aggregate_prior_total": snapshot["aggregate_prior_total"],
            "allocated_total": snapshot["allocated_total"],
            "available_capacity": snapshot["available_capacity"],
            "settled_total": snapshot["settled_total"],
            "pending_amount": snapshot["pending_amount"],
            "waiting_amount": snapshot["waiting_amount"],
            "reinstatement_premium_total": snapshot["reinstatement_premium_total"],
            "overcommitted": snapshot["overcommitted"],
            "waiting_count": len(snapshot["waiting"]),
            "waiting": [claims[key]["claim_number"] for key in snapshot["waiting"]],
            "pending_review_count": len(snapshot["pending_review"]),
            "discrepancy_count": discrepancy_count,
        }

    def _discrepancies(self, records: List[Dict[str, Any]], snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        """记录流水与账本快照之间的差异：未入账、待核对、状态/金额对不上。"""
        claims = snapshot["claims"]
        result: List[Dict[str, Any]] = []
        seen = set()
        for record in records:
            key = str(record["id"])
            seen.add(key)
            state = record["state"]
            payload = record["payload"]
            claim = claims.get(key)
            if state in {"claim_submitted", "calculated", "settled"} and claim is None:
                result.append({"type": "missing_from_ledger", "record_id": record["id"], "claim_number": payload.get("claim_number", ""), "detail": "赔案未入账，需要回填"})
                continue
            if claim is None:
                continue
            if claim["status"] == "pending_review":
                result.append({"type": "pending_review", "record_id": record["id"], "claim_number": claim["claim_number"], "detail": claim["review_reason"] or "回填不齐，停在待核对"})
                continue  # 待核对的赔案只报一条差异，其余核对等人工处理
            if claim["duplicate"]:
                result.append({"type": "duplicate_claim", "record_id": record["id"], "claim_number": claim["claim_number"], "detail": "同一事件下赔案号重复"})
            if state == "settled" and not claim["settled"]:
                result.append({"type": "state_mismatch", "record_id": record["id"], "claim_number": claim["claim_number"], "detail": "记录已结算但账本未结算"})
            if state in {"calculated", "settled"} and abs(float(payload.get("recoverable_amount", 0.0)) - float(claim["recoverable"])) > 0.01:
                result.append({"type": "amount_mismatch", "record_id": record["id"], "claim_number": claim["claim_number"], "detail": "记录摊回%s与账本核定%s不一致" % (payload.get("recoverable_amount"), claim["recoverable"])})
            if state in {"rejected", "withdrawn"} and float(claim["allocated"]) > 0.01:
                result.append({"type": "allocation_leak", "record_id": record["id"], "claim_number": claim["claim_number"], "detail": "已关闭赔案仍占用容量"})
        for key, claim in claims.items():
            if key not in seen:
                result.append({"type": "orphan_fact", "record_id": claim["record_id"], "claim_number": claim["claim_number"], "detail": "账本事实找不到对应记录"})
        if snapshot["overcommitted"]:
            result.append({"type": "capacity_overflow", "record_id": None, "claim_number": "", "detail": "已分配摊回超过事件可用容量"})
        return result

    def event_ledger(self, actor: Actor, event_id: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        snapshot = self._event_snapshot(event_id)
        records = self.repository.records_for_event(event_id)
        discrepancies = self._discrepancies(records, snapshot)
        return {
            "event_id": event_id,
            "snapshot": snapshot,
            "summary": self._event_summary(event_id, snapshot, len(discrepancies)),
            "facts": self.repository.facts_for_event(event_id),
            "discrepancies": discrepancies,
        }

    def rebuild_event(self, actor: Actor, event_id: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_admin(actor)
        snapshot = self.repository.rebuild_snapshot(event_id)
        return {"event_id": event_id, "snapshot": snapshot}

    def backfill(self, actor: Actor) -> Dict[str, Any]:
        """旧数据回填：按审计时间线补齐缺失的账本事实（含赔案版本）。

        时间线缺创建事件、版本断档、状态对不上或提交键冲突时，不臆造事实，
        只追加一条待核对事实，赔案停在待核对状态。
        """
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_admin(actor)
        records = sorted(self.repository.list_records(limit=500), key=lambda item: item["id"])
        existing = self.repository.facts_by_record()
        report: Dict[str, Any] = {"backfilled": [], "pending_review": [], "skipped": []}
        for record in records:
            record_id = record["id"]
            event_id = str(record["payload"].get("event_id", ""))
            have = existing.get(record_id, set())
            expected, error = ledger_mod.expected_facts_from_timeline(record, self.repository.audit_timeline(record_id))
            if error is not None:
                if "claim_pending_review" in have:
                    report["skipped"].append(record_id)
                    continue
                fact = {"record_id": record_id, "reason": error, "claim_number": record["payload"].get("claim_number", "")}
                self.repository.append_facts(event_id, [("claim_pending_review", fact, None)], actor.user_id)
                report["pending_review"].append({"record_id": record_id, "reason": error})
                continue
            missing = [item for item in expected if item[0] not in have]
            if not missing:
                report["skipped"].append(record_id)
                continue
            if "claim_pending_review" in have:
                missing.append(("claim_review_cleared", {"record_id": record_id}, None))
            try:
                self.repository.append_facts(event_id, missing, actor.user_id)
            except Conflict as exc:
                fact = {"record_id": record_id, "reason": str(exc), "claim_number": record["payload"].get("claim_number", "")}
                self.repository.append_facts(event_id, [("claim_pending_review", fact, None)], actor.user_id)
                report["pending_review"].append({"record_id": record_id, "reason": str(exc)})
                continue
            report["backfilled"].append(record_id)
        return report

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        states = self.repository.stats()
        snapshots = self.repository.all_snapshots()
        records = self.repository.list_records(limit=500)
        records_by_event: Dict[str, List[Dict[str, Any]]] = {}
        for record in records:
            records_by_event.setdefault(str(record["payload"].get("event_id", "")), []).append(record)
        totals = {
            "events": 0,
            "capacity_total": 0.0,
            "aggregate_prior_total": 0.0,
            "allocated_total": 0.0,
            "available_capacity": 0.0,
            "settled_total": 0.0,
            "pending_amount": 0.0,
            "waiting_amount": 0.0,
            "reinstatement_premium_total": 0.0,
            "waiting_claims": 0,
            "pending_review_claims": 0,
            "discrepancies": 0,
        }
        seen_events = set()
        for snapshot in snapshots:
            event_id = snapshot["event_id"]
            seen_events.add(event_id)
            snapshot = self._event_snapshot(event_id)
            totals["events"] += 1
            for key in ("capacity_total", "aggregate_prior_total", "allocated_total", "available_capacity", "settled_total", "pending_amount", "waiting_amount", "reinstatement_premium_total"):
                totals[key] = round(totals[key] + float(snapshot[key]), 2)
            totals["waiting_claims"] += len(snapshot["waiting"])
            totals["pending_review_claims"] += len(snapshot["pending_review"])
            totals["discrepancies"] += len(self._discrepancies(records_by_event.get(event_id, []), snapshot))
        # 尚未建账的事件里，已有赔案动作的记录都算作未入账差异。
        for event_id, event_records in records_by_event.items():
            if event_id in seen_events:
                continue
            totals["discrepancies"] += sum(1 for record in event_records if record["state"] in {"claim_submitted", "calculated", "settled"})
        return {"states": states, "ledger": totals}
