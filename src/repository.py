"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE TABLE IF NOT EXISTS treaties (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    treaty_ref TEXT NOT NULL UNIQUE,
                    event_id TEXT NOT NULL,
                    attachment REAL NOT NULL,
                    layer_limit REAL NOT NULL,
                    cession_pct REAL NOT NULL,
                    reinstatement_pct REAL NOT NULL,
                    capacity REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    treaty_id INTEGER NOT NULL REFERENCES treaties(id),
                    claim_ref TEXT NOT NULL,
                    reported_loss REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'api',
                    legacy_record_id INTEGER UNIQUE,
                    review_pending INTEGER NOT NULL DEFAULT 0,
                    review_reason TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'reported',
                    approved_loss REAL,
                    recoverable REAL NOT NULL DEFAULT 0,
                    allocated REAL NOT NULL DEFAULT 0,
                    premium REAL NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 0,
                    alloc_order INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(treaty_id, claim_ref)
                );
                CREATE TABLE IF NOT EXISTS ledger_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    treaty_id INTEGER NOT NULL REFERENCES treaties(id),
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    fact_type TEXT NOT NULL,
                    amount REAL,
                    actor_id TEXT NOT NULL,
                    idempotency_key TEXT,
                    details TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(claim_id, fact_type),
                    UNIQUE(treaty_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS ledger_checkpoints (
                    treaty_id INTEGER PRIMARY KEY REFERENCES treaties(id),
                    last_fact_id INTEGER NOT NULL DEFAULT 0,
                    allocated_total REAL NOT NULL DEFAULT 0,
                    available_capacity REAL NOT NULL DEFAULT 0,
                    waiting_count INTEGER NOT NULL DEFAULT 0,
                    pending_amount REAL NOT NULL DEFAULT 0,
                    premium_due REAL NOT NULL DEFAULT 0,
                    snapshot TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_facts_treaty ON ledger_facts(treaty_id, id);
                CREATE INDEX IF NOT EXISTS idx_claims_treaty ON claims(treaty_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---- 巨灾事件账本 ----

    @staticmethod
    def _fact_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["details"] = json.loads(item["details"])
        return item

    def create_treaty(self, prepared: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT INTO treaties(treaty_ref,event_id,attachment,layer_limit,cession_pct,reinstatement_pct,capacity,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (prepared["treaty_ref"], prepared["event_id"], prepared["attachment"], prepared["layer_limit"], prepared["cession_pct"], prepared["reinstatement_pct"], prepared["capacity"], actor_id, now),
                )
                treaty_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO ledger_checkpoints(treaty_id,last_fact_id,allocated_total,available_capacity,waiting_count,pending_amount,premium_due,snapshot,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (treaty_id, 0, 0.0, prepared["capacity"], 0, 0.0, 0.0, "{}", now),
                )
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("treaty_ref已存在") from exc
        return self.get_treaty(treaty_id)

    def get_treaty(self, treaty_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM treaties WHERE id=?", (treaty_id,)).fetchone()
        if row is None:
            raise NotFound("合约不存在")
        return dict(row)

    def get_treaty_by_ref(self, treaty_ref: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM treaties WHERE treaty_ref=?", (treaty_ref,)).fetchone()
        return dict(row) if row is not None else None

    def list_treaties(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM treaties ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def get_claim(self, claim_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if row is None:
            raise NotFound("赔案不存在")
        return dict(row)

    def find_claim_by_ref(self, treaty_id: int, claim_ref: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM claims WHERE treaty_id=? AND claim_ref=?", (treaty_id, claim_ref)).fetchone()
        return dict(row) if row is not None else None

    def list_claims(self, treaty_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM claims WHERE treaty_id=? ORDER BY id", (treaty_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_review_claims(self, treaty_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM claims WHERE treaty_id=? AND review_pending=1 ORDER BY id", (treaty_id,)).fetchall()
        return [dict(row) for row in rows]

    def count_claims(self, treaty_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS total FROM claims WHERE treaty_id=?", (treaty_id,)).fetchone()
        return int(row["total"])

    def insert_claim_submission(self, treaty_id: int, claim_ref: str, reported_loss: float, actor_id: str, idempotency_key: Optional[str]):
        """插入赔案与submitted事实，同事务提交；重复提交返回既有赔案。"""
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT INTO claims(treaty_id,claim_ref,reported_loss,source,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (treaty_id, claim_ref, reported_loss, "api", actor_id, now, now),
                )
                claim_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO ledger_facts(treaty_id,claim_id,fact_type,amount,actor_id,idempotency_key,details,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (treaty_id, claim_id, "submitted", reported_loss, actor_id, idempotency_key, json.dumps({"claim_ref": claim_ref}, ensure_ascii=False, sort_keys=True), now),
                )
                connection.commit()
            return self.get_claim(claim_id), True
        except sqlite3.IntegrityError:
            existing = self.find_claim_by_ref(treaty_id, claim_ref)
            if existing is None and idempotency_key:
                fact = self.find_fact_by_key(treaty_id, idempotency_key)
                if fact is not None:
                    existing = self.get_claim(fact["claim_id"])
            if existing is None:
                raise Conflict("赔案提交冲突")
            return existing, False

    def get_fact(self, fact_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM ledger_facts WHERE id=?", (fact_id,)).fetchone()
        if row is None:
            raise NotFound("事实不存在")
        return self._fact_row(row)

    def find_fact_by_key(self, treaty_id: int, idempotency_key: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM ledger_facts WHERE treaty_id=? AND idempotency_key=?", (treaty_id, idempotency_key)).fetchone()
        return self._fact_row(row) if row is not None else None

    def insert_fact(self, treaty_id: int, claim_id: int, fact_type: str, amount: Optional[float], actor_id: str, idempotency_key: Optional[str], details: Dict[str, Any]):
        """追加单条事实；幂等键命中返回既有事实，同类事实重复报冲突。"""
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT INTO ledger_facts(treaty_id,claim_id,fact_type,amount,actor_id,idempotency_key,details,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (treaty_id, claim_id, fact_type, amount, actor_id, idempotency_key, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
                )
                fact_id = int(cursor.lastrowid)
                connection.commit()
            return self.get_fact(fact_id), True
        except sqlite3.IntegrityError:
            if idempotency_key:
                fact = self.find_fact_by_key(treaty_id, idempotency_key)
                if fact is not None:
                    return fact, False
            raise Conflict("同类事实已存在，重复提交不再占用")

    def list_facts(self, treaty_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM ledger_facts WHERE treaty_id=? ORDER BY id", (treaty_id,)).fetchall()
        return [self._fact_row(row) for row in rows]

    def max_fact_id(self, treaty_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(id),0) AS m FROM ledger_facts WHERE treaty_id=?", (treaty_id,)).fetchone()
        return int(row["m"])

    def get_checkpoint(self, treaty_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM ledger_checkpoints WHERE treaty_id=?", (treaty_id,)).fetchone()
        return dict(row) if row is not None else None

    def replay_and_persist(self, treaty_id: int, replay_fn) -> Dict[str, Any]:
        """在写锁内读事实、重算并落库投影与检查点，保证并发下结果一致。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            treaty_row = connection.execute("SELECT * FROM treaties WHERE id=?", (treaty_id,)).fetchone()
            if treaty_row is None:
                connection.rollback()
                raise NotFound("合约不存在")
            claims = [dict(row) for row in connection.execute("SELECT * FROM claims WHERE treaty_id=? ORDER BY id", (treaty_id,)).fetchall()]
            facts = [self._fact_row(row) for row in connection.execute("SELECT * FROM ledger_facts WHERE treaty_id=? ORDER BY id", (treaty_id,)).fetchall()]
            projection = replay_fn(dict(treaty_row), claims, facts)
            for claim_id, proj in projection["claims"].items():
                connection.execute(
                    "UPDATE claims SET status=?,approved_loss=?,allocated=?,premium=?,recoverable=?,version=?,alloc_order=?,updated_at=? WHERE id=?",
                    (proj["status"], proj["approved_loss"], proj["allocated"], proj["premium"], proj["recoverable"], proj["version"], proj["order"], now, claim_id),
                )
            connection.execute(
                "INSERT OR REPLACE INTO ledger_checkpoints(treaty_id,last_fact_id,allocated_total,available_capacity,waiting_count,pending_amount,premium_due,snapshot,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    treaty_id,
                    projection["last_fact_id"],
                    projection["allocated_total"],
                    projection["available_capacity"],
                    projection["waiting_count"],
                    projection["pending_amount"],
                    projection["premium_due"],
                    json.dumps(projection["claims"], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            connection.commit()
        return projection

    def legacy_claim_exists(self, legacy_record_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM claims WHERE legacy_record_id=?", (legacy_record_id,)).fetchone()
        return row is not None

    def insert_legacy_claim(self, treaty_id: int, claim_ref: str, reported_loss: float, legacy_record_id: int, review_reason: str, facts: List[Dict[str, Any]], actor_id: str):
        """回填：赔案与重建事实同事务写入，事实保留原审计时间。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT INTO claims(treaty_id,claim_ref,reported_loss,source,legacy_record_id,review_pending,review_reason,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (treaty_id, claim_ref, reported_loss, "backfill", legacy_record_id, 1 if review_reason else 0, review_reason or "", actor_id, now, now),
            )
            claim_id = int(cursor.lastrowid)
            for fact in facts:
                connection.execute(
                    "INSERT INTO ledger_facts(treaty_id,claim_id,fact_type,amount,actor_id,idempotency_key,details,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (treaty_id, claim_id, fact["fact_type"], fact["amount"], fact["actor_id"], fact["idempotency_key"], json.dumps(fact["details"], ensure_ascii=False, sort_keys=True), fact["created_at"]),
                )
            connection.commit()
        return self.get_claim(claim_id), True
