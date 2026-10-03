"""SQLite 表结构与事务访问。"""
import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound
from .ledger import replay


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
                CREATE TABLE IF NOT EXISTS ledger_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    fact_type TEXT NOT NULL,
                    fact TEXT NOT NULL,
                    submission_key TEXT,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(event_id, seq)
                );
                CREATE TABLE IF NOT EXISTS ledger_snapshots (
                    event_id TEXT PRIMARY KEY,
                    seq INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_ledger_event ON ledger_facts(event_id, seq);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_submission ON ledger_facts(event_id, submission_key) WHERE submission_key IS NOT NULL;
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

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], ledger: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                return self._mutate_once(record_id, expected_version, state, payload, actor_id, action, details, ledger)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                last_exc = exc
                time.sleep(0.05 * (attempt + 1))
        raise Conflict("账本写入冲突，请重试") from last_exc

    def _mutate_once(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], ledger: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        now = _now()
        try:
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
                if ledger is not None:
                    # 事实追加与快照重算和记录更新同事务：任何一步失败整体回滚，
                    # 账本停留在最后一份完整快照，可用同一版本号安全重试。
                    self._append_fact_tx(connection, ledger["event_id"], ledger["fact_type"], ledger["fact"], ledger.get("submission_key"), actor_id)
                    self._recompute_tx(connection, ledger["event_id"])
                result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一赔案重复提交，容量只占用一次") from exc
        return self._row(result)

    def _append_fact_tx(self, connection: sqlite3.Connection, event_id: str, fact_type: str, fact: Dict[str, Any], submission_key: Optional[str], actor_id: str) -> int:
        row = connection.execute("SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM ledger_facts WHERE event_id=?", (event_id,)).fetchone()
        seq = int(row["next_seq"])
        connection.execute(
            "INSERT INTO ledger_facts(event_id,seq,fact_type,fact,submission_key,actor_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (event_id, seq, fact_type, json.dumps(fact, ensure_ascii=False, sort_keys=True), submission_key, actor_id, _now()),
        )
        return seq

    def _recompute_tx(self, connection: sqlite3.Connection, event_id: str) -> Dict[str, Any]:
        facts = self._facts_tx(connection, event_id)
        snapshot = replay(facts)
        connection.execute(
            "INSERT INTO ledger_snapshots(event_id,seq,state,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(event_id) DO UPDATE SET seq=excluded.seq,state=excluded.state,updated_at=excluded.updated_at",
            (event_id, int(snapshot["seq"]), json.dumps(snapshot, ensure_ascii=False, sort_keys=True), _now()),
        )
        return snapshot

    @staticmethod
    def _facts_tx(connection: sqlite3.Connection, event_id: str) -> List[Dict[str, Any]]:
        rows = connection.execute("SELECT * FROM ledger_facts WHERE event_id=? ORDER BY seq", (event_id,)).fetchall()
        return [Repository._fact_row(row) for row in rows]

    @staticmethod
    def _fact_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["fact"] = json.loads(item["fact"])
        return item

    def append_facts(self, event_id: str, facts: List[Any], actor_id: str) -> Dict[str, Any]:
        """批量追加事实并在同事务内重算快照（回填/修复用）。"""
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for item in facts:
                    fact_type, fact, submission_key = item
                    self._append_fact_tx(connection, event_id, fact_type, fact, submission_key, actor_id)
                snapshot = self._recompute_tx(connection, event_id)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一赔案重复提交，容量只占用一次") from exc
        return snapshot

    def rebuild_snapshot(self, event_id: str) -> Dict[str, Any]:
        """从事实流重建快照：账本恢复与修复的唯一入口。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = self._recompute_tx(connection, event_id)
            connection.commit()
        return snapshot

    def facts_for_event(self, event_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            return self._facts_tx(connection, event_id)

    def max_fact_seq(self, event_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(seq),0) AS max_seq FROM ledger_facts WHERE event_id=?", (event_id,)).fetchone()
        return int(row["max_seq"])

    def snapshot_for_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM ledger_snapshots WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return None
        snapshot = json.loads(row["state"])
        snapshot["seq"] = int(row["seq"])
        return snapshot

    def all_snapshots(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM ledger_snapshots ORDER BY event_id").fetchall()
        result = []
        for row in rows:
            snapshot = json.loads(row["state"])
            snapshot["seq"] = int(row["seq"])
            snapshot["event_id"] = row["event_id"]
            result.append(snapshot)
        return result

    def facts_by_record(self) -> Dict[int, set]:
        """每条记录已入账的事实类型，回填时用来判断缺哪些事实。"""
        with self._connect() as connection:
            rows = connection.execute("SELECT fact_type, fact FROM ledger_facts").fetchall()
        result: Dict[int, set] = {}
        for row in rows:
            record_id = json.loads(row["fact"]).get("record_id")
            if record_id is None:
                continue
            result.setdefault(int(record_id), set()).add(row["fact_type"])
        return result

    def records_for_event(self, event_id: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM records ORDER BY id").fetchall()
        return [self._row(row) for row in rows if (json.loads(row["payload"]).get("event_id") == event_id)]

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
