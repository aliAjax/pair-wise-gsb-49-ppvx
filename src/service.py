"""业务用例编排、权限检查与审计。"""
import sqlite3
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, LedgerWriteError, PermissionDenied, ValidationError, text
from .ledger import (
    AUDIT_FACT_MAP,
    CLAIM_SUBMIT_ROLES,
    FACT_ROLES,
    LEGACY_STATE_CATEGORY,
    TREATY_CREATE_ROLES,
    WAITING,
    LedgerRules,
    derive_category,
)
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None, ledger_rules: LedgerRules = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)
        self.ledger_rules = ledger_rules or LedgerRules()

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    @staticmethod
    def _require_role(actor: Actor, allowed, message: str) -> None:
        if actor.role != "admin" and actor.role not in allowed:
            raise PermissionDenied(message)

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return {"states": self.repository.stats(), "ledger": self.ledger_stats(actor)}

    # ---- 巨灾事件账本 ----

    def _replay_and_persist(self, treaty_id: int) -> Dict[str, Any]:
        try:
            return self.repository.replay_and_persist(treaty_id, self.ledger_rules.replay)
        except sqlite3.Error as exc:
            raise LedgerWriteError("账本写入失败，可从最后完整账本重试") from exc

    def _ensure_current(self, treaty_id: int) -> None:
        """投影落后于事实时，从最后完整检查点继续重算追平。"""
        checkpoint = self.repository.get_checkpoint(treaty_id)
        if checkpoint is None or int(checkpoint["last_fact_id"]) < self.repository.max_fact_id(treaty_id):
            self._replay_and_persist(treaty_id)

    def create_treaty(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, TREATY_CREATE_ROLES, "角色无权创建再保合约")
        prepared = self.ledger_rules.validate_treaty(data or {})
        return self.repository.create_treaty(prepared, actor.user_id)

    def submit_claim(self, actor: Actor, treaty_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_role(actor, CLAIM_SUBMIT_ROLES, "角色无权提交赔案")
        treaty = self.repository.get_treaty(treaty_id)
        payload = self.ledger_rules.validate_claim(data or {})
        try:
            claim, created = self.repository.insert_claim_submission(
                treaty["id"], payload["claim_ref"], payload["reported_loss"], actor.user_id, payload["idempotency_key"]
            )
        except sqlite3.Error as exc:
            raise LedgerWriteError("赔案提交写入失败，可从最后完整账本重试") from exc
        self._replay_and_persist(treaty["id"])
        view = self._claim_view(self.repository.get_claim(claim["id"]))
        view["duplicate"] = not created
        return view

    def append_fact(self, actor: Actor, claim_id: int, fact_type: str, data: Dict[str, Any], idempotency_key: Optional[str] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        fact_type = text({"fact_type": fact_type}, "fact_type")
        if fact_type not in FACT_ROLES:
            raise ValidationError("未知事实类型")
        self._require_role(actor, FACT_ROLES[fact_type], "角色无权追加该事实")
        claim = self.repository.get_claim(claim_id)
        self._ensure_current(claim["treaty_id"])
        key = idempotency_key.strip() if isinstance(idempotency_key, str) and idempotency_key.strip() else None
        if key:
            existing = self.repository.find_fact_by_key(claim["treaty_id"], key)
            if existing is not None:
                view = self._claim_view(self.repository.get_claim(claim_id))
                view["duplicate"] = True
                view["fact_id"] = existing["id"]
                return view
        claim = self.repository.get_claim(claim_id)
        payload = self.ledger_rules.validate_fact(fact_type, data or {})
        self.ledger_rules.check_fact_transition(claim, fact_type)
        try:
            fact, created = self.repository.insert_fact(
                claim["treaty_id"], claim["id"], fact_type, payload.get("approved_loss"), actor.user_id, key, {"input": dict(data or {})}
            )
        except sqlite3.Error as exc:
            raise LedgerWriteError("事实写入失败，可从最后完整账本重试") from exc
        self._replay_and_persist(claim["treaty_id"])
        view = self._claim_view(self.repository.get_claim(claim["id"]))
        view["duplicate"] = not created
        view["fact_id"] = fact["id"]
        return view

    def rebuild_treaty(self, actor: Actor, treaty_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role != "admin":
            raise PermissionDenied("仅管理员可重建账本")
        self.repository.get_treaty(treaty_id)
        self._replay_and_persist(treaty_id)
        return self.get_treaty_detail(actor, treaty_id)

    @staticmethod
    def _claim_view(claim: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": claim["id"],
            "treaty_id": claim["treaty_id"],
            "claim_ref": claim["claim_ref"],
            "reported_loss": claim["reported_loss"],
            "status": claim["status"],
            "approved_loss": claim["approved_loss"],
            "recoverable": claim["recoverable"],
            "allocated": claim["allocated"],
            "reinstatement_premium": claim["premium"],
            "version": claim["version"],
            "review_pending": bool(claim["review_pending"]),
            "review_reason": claim["review_reason"],
            "source": claim["source"],
        }

    def _treaty_summary(self, treaty: Dict[str, Any]) -> Dict[str, Any]:
        self._ensure_current(treaty["id"])
        checkpoint = self.repository.get_checkpoint(treaty["id"])
        review_claims = self.repository.list_review_claims(treaty["id"])
        allocated = round(float(checkpoint["allocated_total"]), 2)
        available = round(float(checkpoint["available_capacity"]), 2)
        capacity = round(float(treaty["capacity"]), 2)
        pending = round(float(checkpoint["pending_amount"]), 2)
        discrepancies: List[Dict[str, Any]] = []
        for claim in review_claims:
            discrepancies.append({"type": "pending_review", "claim_id": claim["id"], "claim_ref": claim["claim_ref"], "reason": claim["review_reason"]})
        if pending > 0.01:
            discrepancies.append({"type": "unmet_demand", "amount": pending, "waiting_count": int(checkpoint["waiting_count"])})
        if abs(allocated + available - capacity) > 0.01:
            discrepancies.append({"type": "capacity_mismatch", "allocated": allocated, "available": available, "capacity": capacity})
        return {
            "id": treaty["id"],
            "treaty_ref": treaty["treaty_ref"],
            "event_id": treaty["event_id"],
            "attachment": treaty["attachment"],
            "limit": treaty["layer_limit"],
            "cession_pct": treaty["cession_pct"],
            "reinstatement_pct": treaty["reinstatement_pct"],
            "capacity": capacity,
            "allocated_total": allocated,
            "available_capacity": available,
            "waiting_count": int(checkpoint["waiting_count"]),
            "pending_amount": pending,
            "premium_due": round(float(checkpoint["premium_due"]), 2),
            "pending_review_count": len(review_claims),
            "discrepancies": discrepancies,
        }

    def list_treaties(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return [self._treaty_summary(treaty) for treaty in self.repository.list_treaties()]

    def get_treaty_detail(self, actor: Actor, treaty_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        treaty = self.repository.get_treaty(treaty_id)
        summary = self._treaty_summary(treaty)
        claims = self.repository.list_claims(treaty["id"])
        summary["claims"] = [self._claim_view(claim) for claim in claims]
        waiting = [claim for claim in claims if claim["status"] == WAITING]
        waiting.sort(key=lambda claim: (claim["alloc_order"], claim["id"]))
        summary["waiting_queue"] = [
            {"claim_id": claim["id"], "claim_ref": claim["claim_ref"], "approved_loss": claim["approved_loss"], "recoverable": claim["recoverable"], "alloc_order": claim["alloc_order"]}
            for claim in waiting
        ]
        checkpoint = self.repository.get_checkpoint(treaty["id"])
        summary["checkpoint"] = {"last_fact_id": checkpoint["last_fact_id"], "updated_at": checkpoint["updated_at"]}
        return summary

    def treaty_facts(self, actor: Actor, treaty_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get_treaty(treaty_id)
        return self.repository.list_facts(treaty_id)

    def ledger_stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        summaries = [self._treaty_summary(treaty) for treaty in self.repository.list_treaties()]
        stats = {
            "treaties": len(summaries),
            "claims": 0,
            "capacity_total": 0.0,
            "allocated_total": 0.0,
            "available_capacity": 0.0,
            "waiting_count": 0,
            "pending_amount": 0.0,
            "premium_due": 0.0,
            "pending_review_count": 0,
            "discrepancies": [],
        }
        for summary in summaries:
            stats["claims"] += self.repository.count_claims(summary["id"])
            stats["capacity_total"] = round(stats["capacity_total"] + summary["capacity"], 2)
            stats["allocated_total"] = round(stats["allocated_total"] + summary["allocated_total"], 2)
            stats["available_capacity"] = round(stats["available_capacity"] + summary["available_capacity"], 2)
            stats["waiting_count"] += summary["waiting_count"]
            stats["pending_amount"] = round(stats["pending_amount"] + summary["pending_amount"], 2)
            stats["premium_due"] = round(stats["premium_due"] + summary["premium_due"], 2)
            stats["pending_review_count"] += summary["pending_review_count"]
            for item in summary["discrepancies"]:
                stats["discrepancies"].append({"treaty_id": summary["id"], "treaty_ref": summary["treaty_ref"], **item})
        return stats

    # ---- 旧数据回填 ----

    def backfill_legacy(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role != "admin":
            raise PermissionDenied("仅管理员可执行回填")
        report = {"backfilled": 0, "pending_review": 0, "skipped": 0, "items": []}
        for record in self.repository.list_records(limit=500):
            item = self._backfill_record(record, actor)
            report[item["status"]] += 1
            report["items"].append(item)
        return report

    def _backfill_record(self, record: Dict[str, Any], actor: Actor) -> Dict[str, Any]:
        item = {"record_id": record["id"], "reference": record["reference"]}
        timeline = self.repository.audit_timeline(record["id"])
        payload = record["payload"]
        state = record["state"]
        claim_events = [event for event in timeline if event["action"] in AUDIT_FACT_MAP]
        if not claim_events and state not in LEGACY_STATE_CATEGORY:
            item.update(status="skipped", reason="无赔案事件")
            return item
        if self.repository.legacy_claim_exists(record["id"]):
            item.update(status="skipped", reason="已回填")
            return item

        pending_reason = None
        treaty = self.repository.get_treaty_by_ref("LEGACY-" + record["reference"])
        if treaty is None:
            try:
                attachment = float(payload.get("attachment", 0) or 0)
                layer_limit = float(payload.get("limit", 0) or 0)
                cession = float(payload.get("cession_pct", 0) or 0)
                reinstatement = float(payload.get("reinstatement_pct", 0) or 0)
            except (TypeError, ValueError):
                attachment = layer_limit = cession = reinstatement = 0.0
            capacity = round((layer_limit - attachment) * cession, 2) if layer_limit > attachment else 0.0
            if capacity <= 0:
                pending_reason = "合约参数缺失"
            treaty = self.repository.create_treaty(
                {
                    "treaty_ref": "LEGACY-" + record["reference"],
                    "event_id": str(payload.get("event_id") or "LEGACY"),
                    "attachment": attachment,
                    "layer_limit": layer_limit,
                    "cession_pct": cession,
                    "reinstatement_pct": reinstatement,
                    "capacity": capacity,
                },
                actor.user_id,
            )

        submit_event = next((event for event in claim_events if event["action"] == "submit_claim"), None)
        claim_ref = record["reference"]
        if submit_event is not None:
            claim_number = submit_event["details"].get("input", {}).get("claim_number")
            if claim_number:
                claim_ref = claim_number
        try:
            reported_loss = float(payload.get("loss_amount", 0) or 0)
        except (TypeError, ValueError):
            reported_loss = 0.0

        facts: List[Dict[str, Any]] = []
        expected_version = 1
        for event in timeline:
            if int(event["version"]) != expected_version:
                pending_reason = pending_reason or "审计时间线版本缺口"
                break
            expected_version += 1
            action = event["action"]
            if action not in AUDIT_FACT_MAP:
                continue
            amount = None
            if action == "calculate":
                amount = event["details"].get("input", {}).get("approved_loss", payload.get("approved_loss"))
                if amount is None:
                    pending_reason = pending_reason or "缺少核定金额"
                    break
                amount = float(amount)
            facts.append(
                {
                    "fact_type": AUDIT_FACT_MAP[action],
                    "amount": amount,
                    "actor_id": event["actor_id"],
                    "created_at": event["created_at"],
                    "idempotency_key": "legacy-%s-%s" % (record["id"], event["id"]),
                    "details": {"source": "backfill", "audit_action": action, "audit_id": event["id"]},
                }
            )
        if not pending_reason:
            derived = derive_category([fact["fact_type"] for fact in facts])
            expected_category = LEGACY_STATE_CATEGORY.get(state)
            if expected_category is None or derived != expected_category:
                pending_reason = "账本状态与审计时间线不一致"

        claim, _ = self.repository.insert_legacy_claim(
            treaty["id"], claim_ref, reported_loss, record["id"], pending_reason or "", facts, actor.user_id
        )
        self._replay_and_persist(treaty["id"])
        item.update(
            status="pending_review" if pending_reason else "backfilled",
            claim_id=claim["id"],
            treaty_id=treaty["id"],
        )
        if pending_reason:
            item["reason"] = pending_reason
        return item
