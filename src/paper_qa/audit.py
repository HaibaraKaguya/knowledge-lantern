"""审计事实存储 — 独立 audit.db 中的 append-only 审计表（事务安全写）.

记录的事实字段（需求 7）：request_id / user_id / conversation_id / route /
decision(allow|deny) / reason_code / allowed_source_ids(JSON 数组)。
绝不记录 secrets：不落问题原文、不落答案、不落任何口令/密钥/模型载荷。
每次写入在独立事务中完成（短连接 + commit），并发安全（WAL）。
"""

from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_TABLE = "audit_events"
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id        TEXT NOT NULL,
    user_id           TEXT NOT NULL,
    conversation_id   TEXT,
    route             TEXT NOT NULL,
    decision          TEXT NOT NULL CHECK (decision IN ('allow', 'deny')),
    reason_code       TEXT NOT NULL,
    allowed_source_ids TEXT NOT NULL DEFAULT '[]',
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_request   ON {_TABLE}(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_user      ON {_TABLE}(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_conversation ON {_TABLE}(conversation_id);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_request_id() -> str:
    return f"req-{utc_now()}-{uuid.uuid4().hex[:8]}"


class AuditStore:
    """独立审计库。db_path 缺省为 data/audit.db（调用方显式提供更佳）。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(Path(db_path).resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA)

    def record(
        self,
        *,
        request_id: str,
        user_id: str,
        conversation_id: str | None,
        route: str,
        decision: str,
        reason_code: str,
        allowed_source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        """写入一条审计事实（单事务、append-only）。返回该条元信息。"""
        if decision not in ("allow", "deny"):
            raise ValueError(f"decision 必须是 'allow' 或 'deny'，收到: {decision!r}")
        created = utc_now()
        srcs = json.dumps(list(allowed_source_ids or []), ensure_ascii=False)
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            cur = conn.execute(
                f"INSERT INTO {_TABLE} "
                "(request_id, user_id, conversation_id, route, decision, "
                "reason_code, allowed_source_ids, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (request_id, user_id, conversation_id, route, decision,
                 reason_code, srcs, created),
            )
            conn.commit()
            row_id = cur.lastrowid
        finally:
            conn.close()
        return {
            "id": row_id,
            "request_id": request_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "route": route,
            "decision": decision,
            "reason_code": reason_code,
            "allowed_source_ids": srcs,
            "created_at": created,
        }

    def recent(self, limit: int = 50) -> list[dict]:
        """按时间倒序返回最近审计事实（报告/测试/监控用，只读）。"""
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"SELECT * FROM {_TABLE} ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def count(self) -> int:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            return conn.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone()[0]
        finally:
            conn.close()
