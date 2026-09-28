"""IAM 存储 — users / roles / user_roles / user_scopes 的读写与身份加载。

认证不在本包范围；IAMStore 只维护授权相关的身份与范围数据，由服务端在
已认证前提下依据 user_id 加载身份。调用方提供独立数据库（如 iam.db）；
db_path 缺省为内存库（测试友好）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .actions import Action
from .models import Identity, Role, User
from .scopes import (
    Classification,
    SCOPE_TYPES,
    WILDCARD,
    coerce_classification,
)
from .schema import SCHEMA


class IAMError(Exception):
    """IAM 领域错误基类。"""


class UnknownUserError(IAMError):
    """用户不存在或未启用。"""


class UnknownActionError(IAMError, ValueError):
    """roles.actions 中出现未定义的动作名（配置错误，fail-closed）。"""


class InvalidScopeError(IAMError, ValueError):
    """非法的 scope_type / scope_value。"""


class IAMStore:
    """SQLite 支撑的身份/授权存储。db_path 缺省为内存库（测试友好）。"""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = None if db_path is None else Path(db_path)
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(":memory:" if self.db_path is None else str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- 连接生命周期 ----

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "IAMStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 角色 ----

    def create_role(
        self, role_name: str, *, description: str = "", actions: tuple[Action, ...] = ()
    ) -> None:
        """创建（或覆盖）角色。actions 会被严格校验。"""
        self._validate_actions(actions)
        self.conn.execute(
            "INSERT OR REPLACE INTO roles(role_name, description, actions) VALUES(?, ?, ?)",
            (role_name, description, json.dumps([str(a) for a in actions])),
        )
        self.conn.commit()

    def get_role(self, role_name: str) -> Role | None:
        row = self.conn.execute(
            "SELECT role_name, description, actions FROM roles WHERE role_name = ?",
            (role_name,),
        ).fetchone()
        if row is None:
            return None
        return Role(
            role_name=row["role_name"],
            description=row["description"],
            actions=self._parse_actions(row["actions"]),
        )

    def role_allows_action(self, role_name: str, action: Action | str) -> bool:
        role = self.get_role(role_name)
        if role is None:
            return False
        action = self._coerce_action(action)
        return action in role.actions

    def list_roles(self) -> list[Role]:
        rows = self.conn.execute(
            "SELECT role_name, description, actions FROM roles ORDER BY role_name"
        ).fetchall()
        return [
            Role(
                role_name=r["role_name"],
                description=r["description"],
                actions=self._parse_actions(r["actions"]),
            )
            for r in rows
        ]

    # ---- 用户 ----

    def create_user(self, user_id: str, *, user_name: str = "", enabled: bool = True) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO users(user_id, user_name, enabled) VALUES(?, ?, ?)",
            (user_id, user_name, 1 if enabled else 0),
        )
        self.conn.commit()

    def get_user(self, user_id: str) -> User | None:
        row = self.conn.execute(
            "SELECT user_id, user_name, enabled FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return User(user_id=row["user_id"], user_name=row["user_name"], enabled=bool(row["enabled"]))

    def add_user_role(self, user_id: str, role_name: str) -> None:
        if self.get_user(user_id) is None:
            raise IAMError(f"用户不存在: {user_id}")
        if self.get_role(role_name) is None:
            raise IAMError(f"角色不存在: {role_name}")
        self.conn.execute(
            "INSERT OR IGNORE INTO user_roles(user_id, role_name) VALUES(?, ?)", (user_id, role_name)
        )
        self.conn.commit()

    def add_user_scope(self, user_id: str, scope_type: str, scope_value: str) -> None:
        if self.get_user(user_id) is None:
            raise IAMError(f"用户不存在: {user_id}")
        if scope_type not in SCOPE_TYPES:
            raise InvalidScopeError(f"非法 scope_type: {scope_type!r}")
        self._validate_scope_value(scope_type, scope_value)
        self.conn.execute(
            "INSERT OR IGNORE INTO user_scopes(user_id, scope_type, scope_value) VALUES(?, ?, ?)",
            (user_id, scope_type, scope_value),
        )
        self.conn.commit()

    def list_user_scopes(self, user_id: str) -> dict[str, frozenset[str]]:
        rows = self.conn.execute(
            "SELECT scope_type, scope_value FROM user_scopes WHERE user_id = ? "
            "ORDER BY scope_type, scope_value",
            (user_id,),
        ).fetchall()
        out: dict[str, frozenset[str]] = {}
        for r in rows:
            out.setdefault(r["scope_type"], set()).add(r["scope_value"])
        return {k: frozenset(v) for k, v in out.items()}

    # ---- 身份加载（服务端唯一受信任路径）----

    def load_identity(self, user_id: str) -> Identity:
        """从 IAM 库加载身份快照。用户不存在或未启用 → UnknownUserError。

        这是判定与过滤的数据唯一来源；客户端声称的 role/scope JSON 在此被忽略。
        """
        user = self.get_user(user_id)
        if user is None or not user.enabled:
            raise UnknownUserError(f"用户不存在或未启用: {user_id}")

        roles = tuple(
            r["role_name"]
            for r in self.conn.execute(
                "SELECT role_name FROM user_roles WHERE user_id = ? ORDER BY role_name", (user_id,)
            ).fetchall()
        )

        allowed: set[Action] = set()
        for role_name in roles:
            role = self.get_role(role_name)
            if role is None:
                # 悬空角色引用：忽略（最保守），不崩溃。
                continue
            allowed.update(role.actions)

        scopes = self.list_user_scopes(user_id)
        project_scopes = self._resolve_allowlist(scopes.get("project", frozenset()))
        business_domain_scopes = self._resolve_allowlist(scopes.get("business_domain", frozenset()))
        classification_ceiling = self._resolve_classification_ceiling(
            scopes.get("classification", frozenset())
        )

        return Identity(
            user_id=user.user_id,
            user_name=user.user_name,
            enabled=True,
            roles=roles,
            allowed_actions=frozenset(allowed),
            project_scopes=project_scopes,
            business_domain_scopes=business_domain_scopes,
            classification_ceiling=classification_ceiling,
        )

    # ---- 内部辅助 ----

    def _resolve_allowlist(self, values: frozenset[str]) -> frozenset[str] | None:
        """allow-list 解析：空集或含 '*' → None（该维度不限制）；否则为精确值集合。"""
        if not values or WILDCARD in values:
            return None
        return values

    def _resolve_classification_ceiling(self, values: frozenset[str]) -> Classification:
        if WILDCARD in values:
            return Classification.RESTRICTED
        ceilings = {c for v in values if (c := coerce_classification(v)) is not None}
        if not ceilings:
            # 无显式分类授予 → 最保守：仅 public。
            return Classification.PUBLIC
        return max(ceilings, key=lambda c: c.rank)

    def _parse_actions(self, raw: str) -> tuple[Action, ...]:
        try:
            names = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            raise UnknownActionError(f"角色 actions 不是合法 JSON: {raw!r}") from exc
        if not isinstance(names, list):
            raise UnknownActionError(f"角色 actions 应为数组: {raw!r}")
        return tuple(self._coerce_action(n) for n in names)

    def _coerce_action(self, value: Action | str) -> Action:
        if isinstance(value, Action):
            return value
        try:
            return Action(value)
        except ValueError as exc:
            raise UnknownActionError(f"未定义的动作名: {value!r}") from exc

    def _validate_actions(self, actions: tuple[Action, ...]) -> None:
        for a in actions:
            self._coerce_action(a)

    def _validate_scope_value(self, scope_type: str, scope_value: str) -> None:
        if not isinstance(scope_value, str) or not scope_value:
            raise InvalidScopeError("scope_value 必须是非空字符串")
        if scope_type == "classification":
            if scope_value == WILDCARD:
                return
            if coerce_classification(scope_value) is None:
                raise InvalidScopeError(f"非法分类范围值: {scope_value!r}")
