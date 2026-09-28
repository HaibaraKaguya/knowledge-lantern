"""IAM 库 schema（调用方提供的独立数据库，如 iam.db）。

表清单：
- users:       用户主体（不含任何口令/密钥——认证不在本包范围）；
- roles:       角色，actions 为 JSON 数组（可授予的动作名）；
- user_roles:  用户-角色多对多；
- user_scopes: 用户数据范围（project / business_domain / classification）。

classification scope_value 含义：授予该分类，并隐含授予所有更低分类（ceiling 语义）；
'*' 表示 unrestricted（ceiling = restricted）。
project / business_domain scope_value：逐值 allow-list；'*' 表示该维度不限制。
"""

from __future__ import annotations

SCHEMA: str = """
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    user_name  TEXT NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS roles (
    role_name   TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    actions     TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS user_roles (
    user_id   TEXT NOT NULL REFERENCES users(user_id),
    role_name TEXT NOT NULL REFERENCES roles(role_name),
    PRIMARY KEY (user_id, role_name)
);
CREATE TABLE IF NOT EXISTS user_scopes (
    user_id     TEXT NOT NULL REFERENCES users(user_id),
    scope_type  TEXT NOT NULL CHECK (scope_type IN ('project', 'business_domain', 'classification')),
    scope_value TEXT NOT NULL,
    PRIMARY KEY (user_id, scope_type, scope_value)
);
CREATE INDEX IF NOT EXISTS idx_user_roles_user  ON user_roles(user_id);
CREATE INDEX IF NOT EXISTS idx_user_scopes_user ON user_scopes(user_id);
"""
