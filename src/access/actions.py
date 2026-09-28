"""授权动作定义 — 部门知识问答的最小动作集。

支持三个动作：
- INTERNAL_RETRIEVAL_ASK:  知识库（knowledge.db）检索问答；
- KNOWLEDGE_SUBMIT:        向资料工作台提交资料（进入待审核区）；
- KNOWLEDGE_REVIEW:        审核并发布/拒绝他人提交的资料。

动作是最小授权粒度；角色可执行的动作以 JSON 数组存于 roles.actions 列，
由角色授予用户（见 user_roles）。授权判定中动作名是唯一来源，客户端不可覆盖。
"""

from __future__ import annotations

from enum import StrEnum


class Action(StrEnum):
    """授权动作（str 枚举；值即机器可读动作名）。"""

    INTERNAL_RETRIEVAL_ASK = "INTERNAL_RETRIEVAL_ASK"
    KNOWLEDGE_SUBMIT = "KNOWLEDGE_SUBMIT"
    KNOWLEDGE_REVIEW = "KNOWLEDGE_REVIEW"


#: 全部受支持动作，用于确定性校验/遍历。
ALL_ACTIONS: tuple[Action, ...] = tuple(Action)
