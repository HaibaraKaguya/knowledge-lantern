"""会话持久化 — 服务端拥有的会话 + 匿名遗留轮次兼容.

设计：
- 服务端拥有的会话：`conversations` 表持有 conversation_id + owner_user_id；
  每个 turn 记录 conversation_id + owner_user_id，读写都校验归属（只能读写自己的会话）。
- 向后兼容：`paper_qa_turns` 保留 session_id 列；旧的无主轮次（session_id 有值、
  conversation_id/owner_user_id 为 NULL）仍可读写（历史数据，安全迁移：
  ALTER TABLE 加列，不删除任何既有行）。
- 本库只存问答轮次，绝不向此库运行语料 SQL，也绝不写入知识库内容。
"""

from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_TABLE = "paper_qa_turns"
_CONV_TABLE = "conversations"
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_CONV_TABLE} (
    conversation_id TEXT PRIMARY KEY,
    owner_user_id   TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {_TABLE} (
    session_id      TEXT NOT NULL DEFAULT '',
    turn_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    owner_user_id   TEXT,
    question        TEXT NOT NULL,
    answer          TEXT NOT NULL,
    sources_json    TEXT NOT NULL,
    route           TEXT NOT NULL DEFAULT 'INTERNAL_RETRIEVAL',
    model_status    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_paper_qa_turns_session
ON {_TABLE}(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_paper_qa_turns_conversation
ON {_TABLE}(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_owner
ON {_CONV_TABLE}(owner_user_id);
"""

#: paper_qa_turns 在旧库可能缺的列（缺列时安全 ALTER ADD，不动既有数据）。
_TURN_COLUMNS = (
    "conversation_id",
    "owner_user_id",
)


class ConversationNotFoundError(KeyError):
    """会话不存在（读取/写入归属校验失败）。"""


class ConversationOwnershipError(ConversationNotFoundError):
    """会话存在但不属于请求用户（跨用户访问被拒绝，HTTP 层映射 403）。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_conversation_id() -> str:
    return f"conv-{utc_now()}-{uuid.uuid4().hex[:6]}"


class ConversationStore:
    """checkpoints.db 中的服务端会话 + 问答轮次。每次调用打开短连接，线程安全。"""

    def __init__(self, db_path: str | Path):
        self.db_path = str(Path(db_path).resolve())

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        # 迁移顺序必须：先 ALTER 加列，再 executescript 建索引。
        # 旧库（paper_qa_turns 已存在、缺 conversation_id/owner_user_id 列）若先跑
        # executescript，_SCHEMA 中引用 conversation_id 的 CREATE INDEX 会因列不存在
        # 而失败（即使 IF NOT EXISTS 也要求列存在）。故先补列、再建表/索引。
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (_TABLE,),
        ).fetchone() is not None
        if has_table:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({_TABLE})")}
            for c in _TURN_COLUMNS:
                if c not in cols:
                    conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN {c} TEXT")
        conn.executescript(_SCHEMA)
        conn.commit()

    # ---- 会话（服务端拥有）----

    def create_conversation(
        self, owner_user_id: str, conversation_id: str | None = None
    ) -> dict:
        """创建由 owner_user_id 拥有的会话，返回会话元信息。"""
        if not owner_user_id or not owner_user_id.strip():
            raise ValueError("owner_user_id 不能为空")
        cid = conversation_id or _new_conversation_id()
        created = utc_now()
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            conn.execute(
                f"INSERT OR IGNORE INTO {_CONV_TABLE} "
                "(conversation_id, owner_user_id, created_at) VALUES (?,?,?)",
                (cid, owner_user_id, created),
            )
            conn.commit()
        finally:
            conn.close()
        return {"conversation_id": cid, "owner_user_id": owner_user_id, "created_at": created}

    def get_conversation(self, conversation_id: str, owner_user_id: str) -> dict:
        """读取会话；归属不符或不存在的会话抛 ConversationNotFoundError。"""
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            row = conn.execute(
                f"SELECT * FROM {_CONV_TABLE} WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ConversationNotFoundError(conversation_id)
        if row["owner_user_id"] != owner_user_id:
            # 归属不符：明确拒绝（不泄露他人会话内容，但暴露存在性给服务层做 403）。
            raise ConversationOwnershipError(conversation_id)
        return {
            "conversation_id": row["conversation_id"],
            "owner_user_id": row["owner_user_id"],
            "created_at": row["created_at"],
        }

    def list_conversations(self, owner_user_id: str, limit: int = 50) -> list[dict]:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"SELECT * FROM {_CONV_TABLE} WHERE owner_user_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (owner_user_id, limit),
            ).fetchall()
            return [
                {"conversation_id": r["conversation_id"], "owner_user_id": r["owner_user_id"],
                 "created_at": r["created_at"]}
                for r in rows
            ]
        finally:
            conn.close()

    # ---- 轮次 ----

    def add_turn(
        self,
        question: str,
        answer: str,
        sources_json: str,
        *,
        conversation_id: str | None = None,
        owner_user_id: str | None = None,
        session_id: str | None = None,
        model_status: str = "available",
        route: str = "INTERNAL_RETRIEVAL",
    ) -> dict:
        """追加一轮对话。

        - conversation_id + owner_user_id 给定：校验会话归属后写入（服务端会话）。
        - 仅 session_id（遗留匿名）：写入无主轮次（向后兼容）。
        返回该轮元信息。
        """
        created = utc_now()
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            if conversation_id is not None:
                if owner_user_id is None:
                    raise ValueError("提供 conversation_id 时必须同时提供 owner_user_id")
                self.get_conversation(conversation_id, owner_user_id)
            cur = conn.execute(
                f"INSERT INTO {_TABLE} "
                "(session_id, conversation_id, owner_user_id, question, answer, "
                "sources_json, route, model_status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (session_id or "", conversation_id, owner_user_id, question, answer,
                 sources_json, route, model_status, created),
            )
            conn.commit()
            turn_id = cur.lastrowid
        finally:
            conn.close()
        return {
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "owner_user_id": owner_user_id,
            "session_id": session_id,
            "created_at": created,
        }

    def count_turns(self, session_id: str | None = None,
                    conversation_id: str | None = None,
                    owner_user_id: str | None = None) -> int:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            if session_id:
                return conn.execute(
                    f"SELECT COUNT(*) FROM {_TABLE} WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            if conversation_id is not None:
                if owner_user_id is None:
                    raise ValueError("提供 conversation_id 时必须同时提供 owner_user_id")
                self.get_conversation(conversation_id, owner_user_id)
                return conn.execute(
                    f"SELECT COUNT(*) FROM {_TABLE} WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()[0]
            return conn.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone()[0]
        finally:
            conn.close()

    def get_turns(self, session_id: str, limit: int = 20) -> list[dict]:
        """按时间正序返回某遗留会话最近轮次（turn_id 递增即时间递增）。"""
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"SELECT * FROM {_TABLE} WHERE session_id = ? "
                "ORDER BY turn_id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows][::-1]
        finally:
            conn.close()

    def get_conversation_turns(self, conversation_id: str, owner_user_id: str,
                               limit: int = 20) -> list[dict]:
        """读取某服务端会话的轮次（归属校验；不符则抛 ConversationNotFoundError）。"""
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            self.get_conversation(conversation_id, owner_user_id)
            rows = conn.execute(
                f"SELECT * FROM {_TABLE} WHERE conversation_id = ? "
                "ORDER BY turn_id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
            return [self._row_to_dict(r) for r in rows][::-1]
        finally:
            conn.close()

    @staticmethod
    def _row_to_dict(r: sqlite3.Row) -> dict:
        return {
            "turn_id": r["turn_id"],
            "session_id": r["session_id"],
            "conversation_id": r["conversation_id"],
            "owner_user_id": r["owner_user_id"],
            "question": r["question"],
            "answer": r["answer"],
            "sources": json.loads(r["sources_json"] or "[]"),
            "route": r["route"],
            "model_status": r["model_status"],
            "created_at": r["created_at"],
        }
