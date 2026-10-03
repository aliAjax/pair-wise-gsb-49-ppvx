"""巨灾事件账本规则：事实追加、容量重算、等待队列与旧数据回填。

账本以ledger_facts为唯一事实来源，赔案状态与容量分配都是回放结果：
核定、结算、撤回只追加事实，随后按事件顺序重算整份合约的容量分配。
"""
from typing import Any, Dict, List, Optional

from .domain import Conflict, ValidationError, number, optional_text, text


# 赔案投影状态
REPORTED = "reported"            # 已报案，未核定，不占用容量
ALLOCATED = "allocated"          # 已核定且容量已分配
WAITING = "waiting"              # 已核定但容量不足，进入等待队列
SETTLED = "settled"              # 已结算，保留占用
WITHDRAWN = "withdrawn"          # 已撤回，释放占用
PENDING_REVIEW = "pending_review"  # 回填不齐，停在待核对

FACT_TYPES = ("submitted", "approved", "settled", "withdrawn")

TREATY_CREATE_ROLES = {"underwriter"}
CLAIM_SUBMIT_ROLES = {"claims_officer"}
FACT_ROLES = {
    "approved": {"claims_officer"},
    "settled": {"finance"},
    "withdrawn": {"finance", "claims_officer"},
}

EPSILON = 0.01

# 各类事实允许追加的前置状态
FACT_TRANSITIONS = {
    "approved": {REPORTED},
    "settled": {ALLOCATED},
    "withdrawn": {REPORTED, ALLOCATED, WAITING, SETTLED},
}

# 旧records状态到事实类别的映射，用于回填一致性核对
LEGACY_STATE_CATEGORY = {
    "claim_submitted": "reported",
    "calculated": "approved",
    "settled": "settled",
    "rejected": "withdrawn",
}

# 旧审计动作到事实类型的映射
AUDIT_FACT_MAP = {
    "submit_claim": "submitted",
    "calculate": "approved",
    "settle": "settled",
    "reject": "withdrawn",
}


def derive_category(fact_types: List[str]) -> Optional[str]:
    """按事实顺序推导赔案类别，用于回填时与旧记录状态核对。"""
    category = None
    for ftype in fact_types:
        if ftype == "submitted" and category is None:
            category = "reported"
        elif ftype == "approved":
            category = "approved"
        elif ftype == "settled":
            category = "settled"
        elif ftype == "withdrawn":
            category = "withdrawn"
    return category


class LedgerRules:
    """纯函数规则：输入校验、事实转换检查与容量重算。"""

    def validate_treaty(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        p["treaty_ref"] = text(p, "treaty_ref")
        p["event_id"] = text(p, "event_id")
        attachment = number(p, "attachment", 0)
        layer_limit = number(p, "limit", 0)
        p["cession_pct"] = number(p, "cession_pct", 0, 1)
        p["reinstatement_pct"] = number(p, "reinstatement_pct", 0, 1)
        if layer_limit <= attachment:
            raise ValidationError("赔偿限额必须高于起赔点")
        p["attachment"] = attachment
        p["layer_limit"] = layer_limit
        p["capacity"] = round((layer_limit - attachment) * p["cession_pct"], 2)
        return p

    def validate_claim(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        key = optional_text(p, "idempotency_key")
        return {
            "claim_ref": text(p, "claim_ref"),
            "reported_loss": number(p, "reported_loss", 0),
            "idempotency_key": key or None,
        }

    def validate_fact(self, fact_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(data or {})
        if fact_type == "approved":
            return {"approved_loss": number(data, "approved_loss", 0)}
        if fact_type == "settled":
            return {"payment_reference": optional_text(data, "payment_reference")}
        if fact_type == "withdrawn":
            return {"reason": text(data, "reason")}
        raise ValidationError("未知事实类型")

    def check_fact_transition(self, claim: Dict[str, Any], fact_type: str) -> None:
        allowed = FACT_TRANSITIONS.get(fact_type)
        if allowed is None:
            raise ValidationError("未知事实类型")
        status = claim["status"]
        if status in allowed:
            return
        if fact_type == "settled" and status == WAITING:
            raise Conflict("容量不足，赔案在等待队列中")
        if status == PENDING_REVIEW:
            raise Conflict("赔案待核对，请先处理回填差异")
        raise Conflict("当前状态不允许追加%s事实" % fact_type)

    @staticmethod
    def recoverable(treaty: Dict[str, Any], approved_loss: float) -> float:
        width = float(treaty["layer_limit"]) - float(treaty["attachment"])
        loss = max(0.0, float(approved_loss) - float(treaty["attachment"]))
        return round(min(loss, width) * float(treaty["cession_pct"]), 2)

    def replay(self, treaty: Dict[str, Any], claims: List[Dict[str, Any]], facts: List[Dict[str, Any]]) -> Dict[str, Any]:
        """按事件顺序回放全部事实，重算每个赔案的占用与合约可用容量。

        分配顺序取核定事实的先后；已撤回的赔案释放占用，等待中的赔案
        按顺序补入剩余额度。同一输入永远得到同一结果，因此重复提交与
        失败重试都不会重复占用容量。
        """
        states: Dict[int, Dict[str, Any]] = {}
        for claim in claims:
            states[claim["id"]] = {
                "approved_loss": None,
                "settled": False,
                "withdrawn": False,
                "order": 0,
                "version": 0,
            }
        last_fact_id = 0
        for fact in sorted(facts, key=lambda item: item["id"]):
            state = states.get(fact["claim_id"])
            if state is None:
                continue
            state["version"] += 1
            last_fact_id = max(last_fact_id, int(fact["id"]))
            ftype = fact["fact_type"]
            if ftype == "approved":
                state["approved_loss"] = round(float(fact["amount"] or 0.0), 2)
                state["order"] = int(fact["id"])
            elif ftype == "settled":
                state["settled"] = True
            elif ftype == "withdrawn":
                state["withdrawn"] = True

        capacity = round(float(treaty["capacity"]), 2)
        allocated_total = 0.0
        allocations: Dict[int, float] = {}
        approved_order = sorted(
            (cid for cid, st in states.items() if st["approved_loss"] is not None and not st["withdrawn"]),
            key=lambda cid: states[cid]["order"],
        )
        for cid in approved_order:
            demand = self.recoverable(treaty, states[cid]["approved_loss"])
            if allocated_total + demand <= capacity + EPSILON:
                allocations[cid] = demand
                allocated_total = round(allocated_total + demand, 2)
            else:
                allocations[cid] = 0.0

        projections: Dict[int, Dict[str, Any]] = {}
        waiting_count = 0
        pending_amount = 0.0
        premium_due = 0.0
        for claim in claims:
            cid = claim["id"]
            state = states[cid]
            demand = 0.0
            if state["approved_loss"] is not None:
                demand = self.recoverable(treaty, state["approved_loss"])
            if claim["review_pending"]:
                status, allocated = PENDING_REVIEW, 0.0
            elif state["withdrawn"]:
                status, allocated = WITHDRAWN, 0.0
            elif state["approved_loss"] is None:
                status, allocated = REPORTED, 0.0
            else:
                allocated = allocations.get(cid, 0.0)
                if allocated > 0.0 or demand <= EPSILON:
                    status = SETTLED if state["settled"] else ALLOCATED
                else:
                    status = WAITING
                    waiting_count += 1
                    pending_amount = round(pending_amount + demand, 2)
            premium = round(allocated * float(treaty["reinstatement_pct"]), 2)
            premium_due = round(premium_due + premium, 2)
            projections[cid] = {
                "status": status,
                "approved_loss": state["approved_loss"],
                "recoverable": demand,
                "allocated": allocated,
                "premium": premium,
                "version": state["version"],
                "order": state["order"],
            }
        return {
            "claims": projections,
            "capacity": capacity,
            "allocated_total": round(allocated_total, 2),
            "available_capacity": round(capacity - allocated_total, 2),
            "waiting_count": waiting_count,
            "pending_amount": pending_amount,
            "premium_due": premium_due,
            "last_fact_id": last_fact_id,
        }
