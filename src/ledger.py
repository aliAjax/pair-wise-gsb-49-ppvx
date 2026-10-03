"""巨灾事件账本：事实追加、容量重算与旧数据回填的纯函数。

账本模型：
- 每个巨灾事件对应一条只追加的事实流（ledger_facts 表），核定、结算、
  撤回等动作只是往事实流末尾追加一条事实，历史事实永不修改。
- 任意时刻的容量分配都按事实顺序重算得出（replay），快照只是重算结果
  的缓存；写入失败回滚后，可以从最后一份完整快照对应的事实继续重试。
- 容量口径：事件可用容量 = Σ 已绑定合约(层宽×分保比例) − 既往累计占用
  − 已分配摊回。核定顺序在前的赔案先分配，容量不足的进入等待队列；
  已结算赔案撤回后释放额度，等待中的赔案按剩余额度补入。
"""
from typing import Any, Dict, List, Optional, Tuple

from .rules import TRANSITIONS

ROUND = 2
EPSILON = 0.01
INITIAL_STATE = "quoted"

FACT_TYPES = (
    "contract_bound",
    "claim_submitted",
    "claim_assessed",
    "claim_settled",
    "claim_withdrawn",
    "claim_rejected",
    "claim_pending_review",
    "claim_review_cleared",
)

# 记录动作 -> 账本事实类型
ACTION_FACT_TYPES = {
    "bind": "contract_bound",
    "submit_claim": "claim_submitted",
    "calculate": "claim_assessed",
    "settle": "claim_settled",
    "reject": "claim_rejected",
    "withdraw": "claim_withdrawn",
}

CLOSE_FACT_KINDS = {"claim_withdrawn": "withdrawn", "claim_rejected": "rejected"}


def _money(value: Any) -> float:
    return round(float(value), ROUND)


def empty_snapshot() -> Dict[str, Any]:
    """没有任何事实时的空账本。"""
    return {
        "seq": 0,
        "capacity_total": 0.0,
        "aggregate_prior_total": 0.0,
        "allocated_total": 0.0,
        "available_capacity": 0.0,
        "settled_total": 0.0,
        "pending_amount": 0.0,
        "waiting_amount": 0.0,
        "reinstatement_premium_total": 0.0,
        "overcommitted": False,
        "claims": {},
        "waiting": [],
        "pending_review": [],
    }


def _new_claim(record_id: Any) -> Dict[str, Any]:
    return {
        "record_id": record_id,
        "claim_number": "",
        "reported_loss": 0.0,
        "recoverable": 0.0,
        "reinstatement_pct": 0.0,
        "allocated": 0.0,
        "reinstatement_premium": 0.0,
        "claim_version": None,
        "assessed": False,
        "settled": False,
        "closed": False,
        "close_kind": "",
        "payment_reference": "",
        "duplicate": False,
        "pending_review": False,
        "review_reason": "",
        "status": "submitted",
    }


def replay(facts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按事实顺序重算事件账本，返回可JSON序列化的账本快照。

    facts 中的每条事实形如 {"seq": int, "fact_type": str, "fact": dict,
    "submission_key": str|None}，必须已按 seq 升序排列。
    """
    capacity_total = 0.0
    prior_total = 0.0
    contracts: Dict[str, bool] = {}
    claims: Dict[str, Dict[str, Any]] = {}
    assessment_order: List[str] = []
    seen_submissions = set()
    last_seq = 0

    def claim_for(record_id: Any) -> Dict[str, Any]:
        key = str(record_id)
        if key not in claims:
            claims[key] = _new_claim(record_id)
        return claims[key]

    for entry in facts:
        fact_type = entry.get("fact_type")
        data = entry.get("fact") or {}
        last_seq = max(last_seq, int(entry.get("seq", 0)))
        if fact_type == "contract_bound":
            key = str(data.get("record_id"))
            if key in contracts:
                continue  # 同一合约重复绑定不再占用
            contracts[key] = True
            capacity_total += float(data.get("capacity", 0.0))
            prior_total += max(0.0, float(data.get("aggregate_prior", 0.0)))
        elif fact_type == "claim_submitted":
            submission_key = entry.get("submission_key")
            if submission_key and submission_key in seen_submissions:
                continue  # 重复提交不再占用
            if submission_key:
                seen_submissions.add(submission_key)
            claim = claim_for(data.get("record_id"))
            claim["claim_number"] = str(data.get("claim_number", "") or "")
            claim["reported_loss"] = _money(data.get("reported_loss", 0.0))
        elif fact_type == "claim_assessed":
            claim = claim_for(data.get("record_id"))
            claim["recoverable"] = _money(data.get("recoverable", 0.0))
            claim["reinstatement_pct"] = float(data.get("reinstatement_pct", 0.0))
            claim["claim_version"] = data.get("claim_version")
            claim["assessed"] = True
            key = str(data.get("record_id"))
            if key not in assessment_order:
                assessment_order.append(key)
        elif fact_type == "claim_settled":
            claim = claim_for(data.get("record_id"))
            claim["settled"] = True
            claim["payment_reference"] = str(data.get("payment_reference", "") or "")
            if data.get("claim_version") is not None:
                claim["claim_version"] = data.get("claim_version")
        elif fact_type in CLOSE_FACT_KINDS:
            claim = claim_for(data.get("record_id"))
            claim["closed"] = True
            claim["close_kind"] = CLOSE_FACT_KINDS[fact_type]
            if data.get("claim_version") is not None:
                claim["claim_version"] = data.get("claim_version")
        elif fact_type == "claim_pending_review":
            claim = claim_for(data.get("record_id"))
            claim["pending_review"] = True
            claim["review_reason"] = str(data.get("reason", "") or "")
            if data.get("claim_number"):
                claim["claim_number"] = str(data["claim_number"])
        elif fact_type == "claim_review_cleared":
            claim = claim_for(data.get("record_id"))
            claim["pending_review"] = False
            claim["review_reason"] = ""

    # 同一事件下赔案号重复的赔案不参与分配（数据质量差异，进入差异列表）。
    active_numbers: Dict[str, str] = {}
    for key, claim in claims.items():
        if claim["closed"] or claim["pending_review"] or not claim["claim_number"]:
            continue
        owner = active_numbers.get(claim["claim_number"])
        if owner is None:
            active_numbers[claim["claim_number"]] = key
        elif owner != key:
            claim["duplicate"] = True

    # 按核定顺序重新分配容量：已结算的必须保留占用；其余先到先得，
    # 容量不足的进入等待队列，后续撤回释放额度后重算时自动补入。
    remaining = capacity_total - prior_total
    waiting: List[str] = []
    for key in assessment_order:
        claim = claims[key]
        if claim["closed"] or claim["pending_review"] or claim["duplicate"]:
            claim["allocated"] = 0.0
            continue
        recoverable = claim["recoverable"]
        if claim["settled"]:
            claim["allocated"] = recoverable
            remaining -= recoverable
        elif recoverable <= remaining + EPSILON:
            claim["allocated"] = recoverable
            remaining -= recoverable
        else:
            claim["allocated"] = 0.0
            waiting.append(key)

    allocated_total = 0.0
    settled_total = 0.0
    pending_amount = 0.0
    waiting_amount = 0.0
    reinstatement_total = 0.0
    pending_review: List[str] = []
    waiting_set = set(waiting)
    for key, claim in claims.items():
        if claim["pending_review"]:
            pending_review.append(key)
        if claim["closed"] or claim["pending_review"] or claim["duplicate"]:
            claim["allocated"] = 0.0
        claim["reinstatement_premium"] = _money(claim["allocated"] * claim["reinstatement_pct"])
        allocated_total += claim["allocated"]
        if claim["settled"] and not claim["closed"]:
            settled_total += claim["allocated"]
        elif claim["assessed"] and not claim["closed"] and not claim["pending_review"] and not claim["duplicate"]:
            if key in waiting_set:
                waiting_amount += claim["recoverable"]
            else:
                pending_amount += claim["allocated"]
        reinstatement_total += claim["reinstatement_premium"]
        # 状态推导
        if claim["pending_review"]:
            claim["status"] = "pending_review"
        elif claim["closed"]:
            claim["status"] = claim["close_kind"]
        elif not claim["assessed"]:
            claim["status"] = "submitted"
        elif claim["settled"]:
            claim["status"] = "settled"
        elif key in waiting_set:
            claim["status"] = "waiting"
        else:
            claim["status"] = "allocated"
        claim["waiting_position"] = (waiting.index(key) + 1) if key in waiting_set else None

    allocated_total = _money(allocated_total)
    available = _money(capacity_total - prior_total - allocated_total)
    return {
        "seq": last_seq,
        "capacity_total": _money(capacity_total),
        "aggregate_prior_total": _money(prior_total),
        "allocated_total": allocated_total,
        "available_capacity": available,
        "settled_total": _money(settled_total),
        "pending_amount": _money(pending_amount),
        "waiting_amount": _money(waiting_amount),
        "reinstatement_premium_total": _money(reinstatement_total),
        "overcommitted": available < -EPSILON,
        "claims": claims,
        "waiting": waiting,
        "pending_review": sorted(pending_review),
    }


def fact_for_action(action: str, record: Dict[str, Any], new_payload: Dict[str, Any], claim_version: int) -> Optional[Tuple[str, Dict[str, Any], Optional[str]]]:
    """把一次记录动作翻译成账本事实。

    返回 (fact_type, fact, submission_key)；不需要记账的动作返回 None。
    submission_key 用于同一事件内赔案提交的幂等去重。
    """
    payload = new_payload
    record_id = record["id"]
    if action == "bind":
        capacity = _money(float(payload["layer_width"]) * float(payload["cession_pct"]))
        return (
            "contract_bound",
            {
                "record_id": record_id,
                "capacity": capacity,
                "aggregate_prior": _money(payload.get("aggregate_prior", 0.0)),
                "attachment": float(payload["attachment"]),
                "limit": float(payload["limit"]),
                "cession_pct": float(payload["cession_pct"]),
            },
            None,
        )
    if action == "submit_claim":
        claim_number = str(payload["claim_number"])
        submission_key = "claim:%s:%s:%s" % (payload.get("event_id", ""), record_id, claim_number)
        return (
            "claim_submitted",
            {"record_id": record_id, "claim_number": claim_number, "reported_loss": _money(payload.get("loss_amount", 0.0))},
            submission_key,
        )
    if action == "calculate":
        return (
            "claim_assessed",
            {
                "record_id": record_id,
                "approved_loss": _money(payload["approved_loss"]),
                "recoverable": _money(payload["recoverable_amount"]),
                "reinstatement_pct": float(payload["reinstatement_pct"]),
                "claim_version": claim_version,
            },
            None,
        )
    if action == "settle":
        return (
            "claim_settled",
            {
                "record_id": record_id,
                "payment_reference": str(payload["payment_reference"]),
                "amount": _money(payload["recoverable_amount"]),
                "claim_version": claim_version,
            },
            None,
        )
    if action == "reject":
        return ("claim_rejected", {"record_id": record_id, "reason": str(payload.get("reject_reason", "")), "claim_version": claim_version}, None)
    if action == "withdraw":
        return ("claim_withdrawn", {"record_id": record_id, "reason": str(payload.get("withdraw_reason", "")), "claim_version": claim_version}, None)
    return None


def expected_facts_from_timeline(record: Dict[str, Any], timeline: List[Dict[str, Any]]) -> Tuple[List[Tuple[str, Dict[str, Any], Optional[str]]], Optional[str]]:
    """根据审计时间线推导旧数据应有的账本事实（回填用）。

    返回 (facts, None)；时间线缺创建事件、赔案版本断档、状态跳跃无法解释
    或与当前状态对不上时，返回 ([], 错误原因)，调用方应停在待核对。
    """
    if not timeline:
        return [], "审计时间线为空"
    first = timeline[0]
    if first.get("action") != "created":
        return [], "审计时间线缺少创建事件"
    try:
        if int(first.get("version", 0)) != 1:
            return [], "创建事件赔案版本缺失"
    except (TypeError, ValueError):
        return [], "创建事件赔案版本缺失"

    payload = record.get("payload") or {}
    state = INITIAL_STATE
    last_version = 1
    facts: List[Tuple[str, Dict[str, Any], Optional[str]]] = []
    walker = {"id": record["id"], "state": state, "payload": payload}
    for event in timeline[1:]:
        action = event.get("action")
        if action not in ACTION_FACT_TYPES:
            continue  # 备注类事件不产生账本事实
        try:
            version = int(event.get("version"))
        except (TypeError, ValueError):
            return [], "赔案版本缺失，无法回填"
        if version != last_version + 1:
            return [], "赔案版本断档（期望%s，实际%s）" % (last_version + 1, version)
        next_state = TRANSITIONS.get(action, {}).get(state)
        if next_state is None:
            return [], "审计时间线状态跳跃无法解释：%s上执行%s" % (state, action)
        walker["state"] = state
        try:
            built = fact_for_action(action, walker, payload, version)
        except (KeyError, TypeError, ValueError) as exc:
            return [], "赔案字段缺失，无法回填：%s" % exc
        if built is not None:
            facts.append(built)
        state = next_state
        last_version = version
    if state != record.get("state"):
        return [], "审计时间线推导状态%s与当前状态%s不一致" % (state, record.get("state"))
    return facts, None
