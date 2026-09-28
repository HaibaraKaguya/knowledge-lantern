"""数据范围定义:project / business_domain / classification。

分类(classification)是有序的:public < internal < restricted。
用户以"ceiling(上限)"持有分类范围：能访问不高于自己 ceiling 的所有分类。
project 与 business_domain 是逐值授予的 allow-list;'*' 表示不限制该维度。
"""
#role   = 你能做什么
#scope  = 你能对哪些数据做
from __future__ import annotations

from enum import StrEnum

#: user_scopes.scope_type 的合法取值。
SCOPE_TYPES: tuple[str, ...] = ("project", "business_domain", "classification")

#: 通配范围值：授予后表示该维度不限制。
WILDCARD: str = "*"


class Classification(StrEnum):
    """资源分类（有序：public < internal < restricted）。"""

    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"

    @property
    def rank(self) -> int:
        """确定性序数：public=0 < internal=1 < restricted=2。"""
        return _RANK[self]


_RANK: dict[Classification, int] = {
    Classification.PUBLIC: 0,
    Classification.INTERNAL: 1,
    Classification.RESTRICTED: 2,
}

#: 升序的稳定顺序，用于确定性遍历与过滤值排序。
_CLASS_ORDER: tuple[Classification, ...] = (
    Classification.PUBLIC,
    Classification.INTERNAL,
    Classification.RESTRICTED,
)


def coerce_classification(value: "str | Classification | None") -> Classification | None:
    """把字符串/枚举归一为 Classification；无法识别时返回 None（调用方应 fail-closed）。"""
    if isinstance(value, Classification):
        return value
    if isinstance(value, str):
        try:
            return Classification(value)
        except ValueError:
            return None
    return None


def ordered_up_to(ceiling: Classification) -> tuple[Classification, ...]:
    """返回 <= ceiling 的升序分类序列（含边界）。"""
    return tuple(c for c in _CLASS_ORDER if c.rank <= ceiling.rank)
