"""HTTP 请求模型 — FastAPI 校验。

demo identity selection contract（无真实认证）：
- 客户端在 user_id 字段显式声明 demo 身份（employee / accountant / finance_supervisor），
  服务端从 iam.db 构建不可变 AccessContext，绝不信任客户端伪造的 role/scope；
- conversation_id 缺省 → 服务端为该 user_id 新建会话并回传；
- 审核请求不携带 reviewer 字段 —— 审核人恒为 user_id，结构上杜绝伪造审核人。

路由契约（禁止用户手选）：
- route 恒为服务端自动选择（INTERNAL_RETRIEVAL 检索作答 / DIRECT 直接作答）；
  请求模型不提供 route 字段，且 unknown 字段一律 422（extra=forbid），
  客户端无法干预路由；DIRECT 是否命中由服务端模型路由器在检索前决定。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .material import DEFAULT_DEPARTMENT, DEPARTMENTS


class PaperQAAskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., description="用户问题（去除首尾空白后校验非空）")
    user_id: str | None = Field(
        default=None, description="demo 身份选择，服务端从 iam.db 解析权限"
    )
    conversation_id: str | None = Field(
        default=None, description="服务端会话 ID；缺省自动为该用户创建"
    )
    session_id: str | None = Field(
        default=None, description="遗留匿名会话 ID（仅兼容旧客户端，优先 conversation_id）"
    )
    debug: bool = Field(
        default=False,
        description=(
            "请求完整 retrieval_trace（测试/debug）。仅当服务端显式开启 debug 配置"
            "（SRC_RETRIEVAL_TRACE/retrieval.trace.enabled）或调用为本地测试身份时生效；"
            "普通用户即便传 true 也只得到简洁响应。"
        ),
    )
    # 注意：无 route 字段 —— 路由由服务端模型路由器自动选择，禁止用户手选。


class PaperQAConversationCreateRequest(BaseModel):
    user_id: str = Field(..., description="会话属主（demo 身份选择）")


class KnowledgeIngestRequest(BaseModel):
    """资料提交请求（会计；进入待审核区，不经审核不进入检索）。"""

    user_id: str = Field(..., description="提交者（demo 身份；必须拥有 KNOWLEDGE_SUBMIT）")
    title: str = Field(..., description="资料标题")
    content: str = Field(..., description="资料正文")
    classification: str = Field(
        default="internal",
        description="public | internal | restricted（服务端会按提交者权限校验）",
    )
    department: str = Field(
        default=DEFAULT_DEPARTMENT,
        description=f"所属部门（五部门之一: {' / '.join(DEPARTMENTS)}）",
    )
    document_id: str | None = Field(
        default=None, description="可选；缺省由服务端生成唯一 ID"
    )
    source: str = Field(default="", description="来源说明（可选）")
    version: str = Field(default="1", description="版本号")


class KnowledgeReviewRequest(BaseModel):
    """审核请求（主管；审核人恒为 user_id，结构上不可伪造）。"""

    user_id: str = Field(..., description="审核人（必须拥有 KNOWLEDGE_REVIEW）")
    note: str = Field(default="", description="审核意见（发布/拒绝理由，可选）")
