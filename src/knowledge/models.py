"""企业知识检索 — 核心数据模型.

所有检索结果与图谱元素都保留出处字段，保证可回溯到
document_id / page / version / status。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List

# 文档状态白名单
STATUSES = ("draft", "pending_review", "active", "rejected", "archived")

# 文档授权分类白名单（与 src/access.scopes.Classification 对齐: public < internal < restricted）
CLASSIFICATIONS = ("public", "internal", "restricted")

# ── 元数据契约分层（重要） ────────────────────────────────────────────
# 本模块把一度被混用的 `department` 收敛为三层互不替代的契约：
#
#   A. 授权层（服务端授权专用，唯一事实源 = documents 表列）:
#        classification / access_scope / project_id / business_domain / status
#      —— 只有这一层参与“能否检索”的判定。绝不使用 department、适用部门或
#         业务分类作为权限依据。
#   B. 内容适用范围层（与授权无关，供业务按钮进入后、检索前过滤）:
#        applicable_departments（多选列表）/ owner_department（维护归属）/
#        business_category（业务/场景分类）
#   C. 描述性继承层（chunk/图谱快照，便于展示与离线审计）:
#        META_FIELDS + 上述 A/B 层的只读快照
#
# `department` 是历史字段，保留以便旧数据/旧测试可读；新数据与新 chunk 必须
# 同时写出 B 层的新字段（applicable_departments 等），不得只写 department。
#
# 中文逐字段说明（看不惯英文字段名先读这份）：
#   data/knowledge_metadata_guide.zh.md
# 机器可读数据字典（脚本生成）：
#   data/knowledge_metadata_dictionary.json

# 授权范围键（access_scope）。语义：服务端授权使用的“范围键”，独立于内容适用范围。
#   company_public      → 全公司可检索（等价 classification='public' 且 project_id IS NULL）
#   <project_id> 等受控键 → 受控项目/账套范围（种子语料固定 'finance-ledger'）
# access_scope 只用于授权，不表达“内容适用于哪个部门”（那是 applicable_departments）。
ACCESS_SCOPE_COMPANY_PUBLIC = "company_public"

# ── 部门注册表（内容适用范围用；与授权用 business_domain 不同维度）────────
# applicable_departments / owner_department 使用本注册表的稳定键，而不是权限字段。
DEPARTMENT_REGISTRY: dict[str, str] = {
    "business_analysis": "经营分析",
    "finance": "财务",
    "human_resources": "人力资源",
    "project_operations": "项目与运营",
    "administration": "行政",
}
DEPARTMENT_KEYS: tuple[str, ...] = tuple(DEPARTMENT_REGISTRY)

# 业务/场景分类白名单（business_category）。
BUSINESS_CATEGORIES: tuple[str, ...] = (
    "business_analysis",     # 经营分析
    "finance_compliance",    # 财务合规（业务分类，不是部门，也不是权限）
    "human_resources",       # 人力资源
    "project_operations",    # 项目与运营
    "administration",        # 行政
)

# ── 内容适用范围契约字段（授权无关）────────────────────────────────────
# chunk / 图节点 / 图边均需继承这些字段（快照），用于“业务按钮进入后、检索前”
# 的适用部门过滤；任何权限判定都不得读取本组字段。
APPLICABILITY_FIELDS: tuple[str, ...] = (
    "applicable_departments",  # list[str] 多选适用部门（部门注册表键）
    "owner_department",        # str 维护归属部门（部门注册表键）
    "business_category",       # str 业务/场景分类（BUSINESS_CATEGORIES）
)

#: 授权契约字段（服务端授权专用；顺序即 documents 过滤契约顺序）。
AUTH_FIELDS: tuple[str, ...] = (
    "classification", "access_scope", "project_id", "business_domain", "status",
)

# ── 五类旧种子标签 → 新契约的可审阅映射 ───────────────────────────────
# 规则说明（写入审计报告与数据字典）：
#   * 旧 `department` 标签一律先映射为内容适用范围（applicable_departments）、
#     维护归属（owner_department）与业务分类（business_category）三者。
#   * 「财务合规」**不是部门、也不是权限字段**：它是业务分类
#     business_category='finance_compliance'，其维护归属映射到财务部门键
#     'finance'，适用部门映射到 ['finance', 'business_analysis']。
#     其“内部/受限”权限只由 classification + access_scope + project_id +
#     business_domain 表达，绝不使用「财务合规」作权限替代。
#   * 「财务合规」适用部门取多值，用于验证适用部门的多选能力（合成归类规则，
#     不代表真实组织架构）。
LEGACY_DEPARTMENT_CONTRACT: dict[str, dict] = {
    "经营分析": {
        "owner_department": "business_analysis",
        "business_category": "business_analysis",
        "applicable_departments": ("business_analysis",),
    },
    "财务合规": {
        "owner_department": "finance",
        "business_category": "finance_compliance",
        # 多选：财务合规内容同时适用于财务与经营分析职能（合成归类规则）
        "applicable_departments": ("finance", "business_analysis"),
    },
    "人力资源": {
        "owner_department": "human_resources",
        "business_category": "human_resources",
        "applicable_departments": ("human_resources",),
    },
    "项目与运营": {
        "owner_department": "project_operations",
        "business_category": "project_operations",
        "applicable_departments": ("project_operations",),
    },
    "行政": {
        "owner_department": "administration",
        "business_category": "administration",
        "applicable_departments": ("administration",),
    },
}

#: 「财务合规」分类规则（机器可读，写入审计报告）。
FINANCE_COMPLIANCE_RULE: dict = {
    "legacy_label": "财务合规",
    "is_department": False,
    "is_permission": False,
    "business_category": "finance_compliance",
    "owner_department": "finance",
    "applicable_departments": ("finance", "business_analysis"),
    "rule": (
        "「财务合规」是业务分类（business_category=finance_compliance），不是部门、"
        "也不是权限字段；其维护归属为 finance，适用范围为 [finance, business_analysis]；"
        "内部/受限权限只由 classification + access_scope + project_id + business_domain 表达。"
    ),
}


def derive_access_scope(classification: str, project_id: str | None) -> str:
    """由分类/项目派生授权范围键（access_scope）。幂等、确定性。

    - public 且无项目限定 → 'company_public'（全公司可检索）；
    - 其余（受控项目/内部/受限）→ 以 project_id 作为受控范围键；
    - 非 public 且无 project_id（授权范围缺失）→ 'unscoped_<classification>'
      （显式标记缺口，检索应 fail-closed）。
    """
    if (classification or "public") == "public" and not project_id:
        return ACCESS_SCOPE_COMPANY_PUBLIC
    if project_id:
        return str(project_id)
    return f"unscoped_{classification or 'unknown'}"


def normalize_applicable_departments(value) -> list[str]:
    """把适用部门归一为“去重保序的部门注册表键列表”。

    - None/空 → []（未知，不伪造）；
    - 字符串 → 单元素列表（兼容旧式单值写法）；
    - 非法键（不在 DEPARTMENT_REGISTRY）保留原值但排到末尾，交由审计标记。
    """
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    else:
        try:
            items = list(value)
        except TypeError:
            items = [value]
    out: list[str] = []
    for item in items:
        key = str(item).strip()
        if key and key not in out:
            out.append(key)
    return out


# ── 阶段 1 检索/图谱必备的继承元数据字段 ─────────────────────────────
# 文档、chunk、图节点与图边都必须携带（无真实值时给出清楚标明为演示的通用值）。
# 授权相关字段（classification/project_id/business_domain/status）以 documents
# 表为单一事实源；其余描述性字段在文档级 metadata 定义，写入/读取时快照继承到
# chunk 与图谱行。META_FIELDS 给出稳定顺序（JSON 输出可复现）。
# 注意：`department` 为历史/兼容字段（值=旧部门标签），其语义已由
# APPLICABILITY_FIELDS（applicable_departments/owner_department/business_category）
# 取代；META_FIELDS 与既有测试契约保持稳定，新契约以独立元组声明。
META_FIELDS: tuple[str, ...] = (
    "department",            # 历史/兼容：旧部门标签（新数据请用 APPLICABILITY_FIELDS）
    "classification",        # public/internal/restricted（真实）
    "project_id",            # 授权项目（None=无项目限定）
    "business_domain",       # 业务域
    "module",                # 所属系统/模块（演示条目=构建器真实分配）
    "submodule",             # 子模块/功能区
    "document_type",         # 文档类型（operation_guide/troubleshooting/.../generic）
    "applicable_role",       # 适用角色（种子条目为通用角色，无真实组织架构）
    "question_type",         # 问题类型（how_to/troubleshooting/...）
    "system_version",        # 系统版本（无真实值→通用占位，标明 demo）
    "error_code",            # 报错码（无则空串）
    "status",                # 文档状态
    "demo",                  # 是否演示条目（True 表示内容/缺省值仅为演示）
)

# ── 数据出处（provenance）字段 ───────────────────────────────────────
# 与 META_FIELDS 分离：META_FIELDS 是检索/图谱必需的继承元数据契约；
# PROVENANCE_FIELDS 回答“这批内容从哪来、能不能当真实公司数据用”。
# 测试期种子知识固定 origin=seeded_synthetic_or_curated 且
# is_real_company_data=False —— 明确不是任何真实公司的制度/经营/财务数据。
PROVENANCE_FIELDS: tuple[str, ...] = (
    "origin",                # seeded_synthetic_or_curated / user_submitted / unspecified
    "license",               # 许可或权利说明（无第三方内容时如实标注）
    "is_real_company_data",  # True/False/None（None=未知，绝不当作真实公司数据）
    "seed_label",            # 人类可读标签（如“测试期种子知识”）
)

#: 测试期种子知识的固定出处取值。
SEED_ORIGIN = "seeded_synthetic_or_curated"
SEED_LICENSE = "project-generated-synthetic-no-third-party"
SEED_LABEL = "测试期种子知识"
SEED_PROVENANCE: dict = {
    "origin": SEED_ORIGIN,
    "license": SEED_LICENSE,
    "is_real_company_data": False,
    "seed_label": SEED_LABEL,
}

#: 非种子条目（如用户提交资料）的出处缺省值：来源未知、不声明许可、真实性未知。
DEFAULT_PROVENANCE: dict = {
    "origin": "unspecified",
    "license": "unspecified",
    "is_real_company_data": None,
    "seed_label": "",
}

#: 真实资料入库时允许的出处取值（未来真实资料以显式 provenance 写入）。
ORIGIN_USER_SUBMITTED = "user_submitted"          # 用户通过资料工作台提交
ORIGIN_CONTROLLED_IMPORT = "controlled_import"    # 受控批量导入
REAL_MATERIAL_ORIGINS: tuple[str, ...] = (
    ORIGIN_USER_SUBMITTED, ORIGIN_CONTROLLED_IMPORT,
)

#: 真实资料入库必填字段（origin ∈ REAL_MATERIAL_ORIGINS 时强制校验）。
#: 说明见 README 数据章节与 data/knowledge_metadata_dictionary.json。
REAL_MATERIAL_REQUIRED_FIELDS: tuple[str, ...] = (
    "document_id", "title", "version", "status",
    "classification", "access_scope", "business_domain",
    "owner_department", "business_category", "applicable_departments",
    "origin", "license", "is_real_company_data",
)

#: chunk / 图节点 / 图边必须继承的完整契约字段（去重保序）。
#: = META_FIELDS（描述+授权快照） + AUTH_FIELDS 中未收录的 access_scope
#:   + APPLICABILITY_FIELDS + PROVENANCE_FIELDS。
CHUNK_CONTRACT_FIELDS: tuple[str, ...] = tuple(dict.fromkeys(
    META_FIELDS + ("access_scope",) + APPLICABILITY_FIELDS + PROVENANCE_FIELDS
))

# 图节点类型白名单（GraphRAG 扩展的主题类型）
GRAPH_NODE_TYPES: tuple[str, ...] = (
    "department",    # 部门根
    "module",        # 系统/模块
    "submodule",     # 子模块/功能区
    "process",       # 流程节点
    "error_code",    # 报错码
    "role",          # 适用角色
    "article",       # 知识条目（文档本身）
)

# 图谱关系谓词白名单（与 metadata 里声明的至少覆盖项一致）
GRAPH_PREDICATES: tuple[str, ...] = (
    "belongs_to",     # 子节点归属父节点（submodule→module→department）
    "applies_to",     # 规则/知识适用的对象（article→role）
    "dependency",     # subject 依赖 object（流程后置步骤依赖前置步骤）
    "precondition",   # subject 的前置条件为 object
    "cause",          # subject 的成因为 object（报错码→原因）
    "solution",       # subject 的解决方案为 object（报错码→处理建议）
)


@dataclass
class Entity:
    """命名实体（企业术语 / 系统 / 客户 / 流程等）。"""
    name: str
    type: str = ""
    description: str = ""


@dataclass
class Relation:
    """显式关系，带证据字段（命中的 chunk、页码、原文引用）。"""
    subject: str
    predicate: str
    object: str
    evidence_chunk_ids: List[str] = field(default_factory=list)
    evidence_page: int = 1
    evidence_quote: str = ""


@dataclass
class Document:
    """一份企业文档（元数据 JSON + 正文 content）。

    授权元数据（classification/access_scope/project_id/business_domain/status）
    在文档级定义，是块检索继承授权的单一事实源；chunks 不复制这些字段。
    内容适用范围（applicable_departments/owner_department/business_category）
    与授权无关，仅用于业务按钮进入后的检索前过滤与展示。
    """
    document_id: str
    title: str
    version: str
    status: str = "active"
    classification: str = "public"
    project_id: str | None = None
    business_domain: str = "research"
    department: str = ""
    owner: str = ""
    source: str = ""
    pages: int = 1
    created: str = ""
    updated: str = ""
    content: str = ""
    # ── 内容适用范围（授权无关；见模块头契约分层）──────────────────
    applicable_departments: List[str] = field(default_factory=list)
    owner_department: str = ""
    business_category: str = ""
    # ── 授权范围键（服务端授权专用；空值时由 classification/project_id 派生）──
    access_scope: str = ""
    entities: List[Entity] = field(default_factory=list)
    relations: List[Relation] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    graph_nodes: List[GraphNode] = field(default_factory=list)
    graph_edges: List[GraphEdge] = field(default_factory=list)
    submitter_id: str = ""
    reviewer_id: str = ""
    submitted_at: str = ""
    reviewed_at: str = ""
    published_at: str = ""
    content_hash: str = ""
    approval_note: str = ""


@dataclass
class Chunk:
    """结构优先切分后的文本块，携带文档出处。

    metadata 是文档级描述性元数据（含 META_FIELDS）在切分时的继承快照，
    由 KnowledgeStore 持久化为 chunks.metadata_json。注意：授权判定永远以
    documents 表当前列为单一事实源（JOIN 继承），本快照不用于授权判定。
    """
    chunk_id: str            # f"{document_id}:{page}:{seq}"
    document_id: str
    page: int
    version: str
    status: str
    heading_path: str        # 例如 "1. 概述 > 1.1 目标"
    seq: int
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class GraphNode:
    """图节点（图谱行由 store.ingest 持久化，携带继承元数据快照）。

    授权：access 判定经 document_id JOIN documents 当前列为准；
    meta 仅为构建时的描述性快照（含 META_FIELDS），不是授权来源。
    """
    node_id: str             # f"{document_id}:{node_type}:{name}"（文档内唯一）
    name: str
    node_type: str
    document_id: str
    description: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class GraphEdge:
    """图边（图谱行由 store.ingest 持久化）。

    predicate 取自 GRAPH_PREDICATES；两端节点属于同一文档（演示图按文档内构建），
    证据块 evidence_chunk_id 指向该文档的具体 chunk。
    """
    edge_id: str             # f"{document_id}:e{seq:03d}"
    document_id: str
    source_node_id: str
    target_node_id: str
    predicate: str
    evidence_chunk_id: str = ""
    description: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class SearchResult:
    """一次检索命中的块 + 文档出处 + 文档级授权元数据。

    授权元数据由检索层从 documents 表 JOIN 继承而来（不复制到 chunk），
    通过 authorization() 暴露为检索智能体消费的确定形状。
    """
    chunk_id: str
    text: str
    heading_path: str
    page: int
    document_id: str
    title: str
    version: str
    status: str
    score: float = 0.0
    match_type: str = ""     # "exact" | "fts" | "exact+fts"
    classification: str = "public"
    project_id: str | None = None
    business_domain: str = "research"
    department: str = ""     # 历史/兼容字段
    access_scope: str = ACCESS_SCOPE_COMPANY_PUBLIC
    applicable_departments: List[str] = field(default_factory=list)
    owner_department: str = ""
    business_category: str = ""
    meta: dict = field(default_factory=dict)  # 描述性继承元数据快照（非授权来源）

    def provenance(self) -> dict:
        """统一的出处字典：document_id/page/version/status。"""
        return {
            "document_id": self.document_id,
            "page": self.page,
            "version": self.version,
            "status": self.status,
        }

    def authorization(self) -> dict:
        """检索面向的授权元数据形状（单一事实源：documents 表）。

        {document_id, classification, access_scope, project_id, business_domain, status}
        —— 只含授权字段；内容适用范围见 content_applicability()。
        """
        return {
            "document_id": self.document_id,
            "classification": self.classification,
            "access_scope": self.access_scope,
            "project_id": self.project_id,
            "business_domain": self.business_domain,
            "status": self.status,
        }

    def content_applicability(self) -> dict:
        """内容适用范围形状（授权无关）：供业务按钮进入后、检索前过滤。

        {applicable_departments, owner_department, business_category}
        —— 这些字段绝不参与权限判定。
        """
        return {
            "applicable_departments": list(self.applicable_departments or []),
            "owner_department": self.owner_department,
            "business_category": self.business_category,
        }

    def data_provenance(self) -> dict:
        """数据出处形状（描述性，非授权）：这批内容从哪来、能否当真实公司数据用。

        与 provenance() 不同：provenance() 是检索出处（document_id/page/version/
        status），本方法是内容来源声明。授权判定不使用本方法结果。
        """
        meta = self.meta or {}
        return {
            "origin": meta.get("origin", "unspecified"),
            "license": meta.get("license", "unspecified"),
            "is_real_company_data": meta.get("is_real_company_data"),
            "seed_label": meta.get("seed_label", ""),
        }
