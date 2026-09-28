#!/usr/bin/env python
"""可重复运行的部门知识库构建脚本 —— 生成「测试期种子知识」。

产出：五类旧部门标签（经营分析 / 财务合规 / 人力资源 / 项目与运营 / 行政）
× 220 条 = 1100 条知识 chunk（每条 = 一份文档，正文经结构切分恰好 1 个 chunk）。

元数据契约（本构建器写出新契约；`department` 仅为历史/兼容字段）：
  授权层（服务端授权专用）: classification / access_scope / project_id /
      business_domain / status
  适用范围层（授权无关）: applicable_departments（多选列表）/ owner_department /
      business_category
  描述层: module / submodule / document_type / applicable_role / question_type /
      system_version / error_code 等（见 META_FIELDS）
  出处层: origin / license / is_real_company_data / seed_label
旧标签 → 新契约映射见 knowledge.models.LEGACY_DEPARTMENT_CONTRACT；其中
「财务合规」是业务分类（finance_compliance），不是部门、也不作权限替代。

性质声明（重要）：
  这些条目是**项目自建的合成/整理测试语料**，不是任何真实公司的制度、
  经营或财务数据。每条携带 PROVENANCE_FIELDS：
    origin=seeded_synthetic_or_curated（测试期种子知识）
    is_real_company_data=False（明确非真实公司数据）
    license=project-generated-synthetic-no-third-party（无第三方内容）
  正文与 metadata 均不含真实公司名/组织架构/真实数值。真实资料须以
  origin=user_submitted / controlled_import 另行走受控导入（见 README 数据章节）。

授权约定（与 src/access 检索过滤一致）：
  - classification=public   → project_id=None（全局公共逃逸口，全员可检索）；
    access_scope=company_public；
  - classification=internal / restricted → project_id='finance-ledger'、
    business_domain='finance'、access_scope='finance-ledger'
    （仅 财务会计/财务主管 的 IAM 范围可检索）。
  - 受限/内部条目的数值与口径均为合成占位，正文注明不代表真实数据。

安全重建方式（幂等，可重复运行）：
  .venv/bin/python scripts/build_demo_knowledge.py --db data/knowledge.db
  # --force 才会先删除目标库文件后重建；不带 --force 时在既有库上增量覆盖
  # （INSERT OR REPLACE + 删除旧 chunk，ID/内容一致，非破坏性）。
每次运行后写出 data/knowledge_import_report.json 供验证。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC = str(_PROJECT_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from knowledge.models import (  # noqa: E402
    Document, GraphNode, GraphEdge, APPLICABILITY_FIELDS, LEGACY_DEPARTMENT_CONTRACT,
    FINANCE_COMPLIANCE_RULE, PROVENANCE_FIELDS, SEED_LABEL,
    SEED_LICENSE, SEED_ORIGIN, SEED_PROVENANCE, derive_access_scope,
)
from knowledge.metadata import inherit_metadata  # noqa: E402
from knowledge.store import KnowledgeStore  # noqa: E402
from knowledge.migrations import SEED_SOURCE  # noqa: E402

# ── 部门定义 ───────────────────────────────────────────────────────────
# internal/restricted：该部门中被授权为“内部/受限财务知识”的条数（finance 范围）。
DEPT_DEFS: tuple[dict, ...] = (
    {
        "key": "BA", "name": "经营分析", "domain": "business_analysis",
        "internal": 30, "restricted": 10,
        "modules": ("经营分析平台", "BI 看板", "指标中心", "预测引擎", "多维分析"),
        "err_prefix": "BA-ERR",
        # 通用角色（无真实组织架构；正文与 metadata 均无真实公司信息）
        "roles": ("经营分析业务人员", "经营分析系统管理员", "经营分析主管"),
    },
    {
        "key": "FC", "name": "财务合规", "domain": "finance",
        "internal": 45, "restricted": 25,
        "modules": ("总账系统", "费用报销系统", "应付系统", "固定资产系统", "银企直连"),
        "err_prefix": "FC-ERR",
        "roles": ("报销申请人", "财务核算岗", "财务主管"),
    },
    {
        "key": "HR", "name": "人力资源", "domain": "hr",
        "internal": 0, "restricted": 0,
        "modules": ("HRM 人事系统", "考勤系统", "招聘门户", "绩效系统", "培训平台"),
        "err_prefix": "HR-ERR",
        "roles": ("人事业务专员", "HRM 系统管理员", "人事主管"),
    },
    {
        "key": "PO", "name": "项目与运营", "domain": "operations",
        "internal": 0, "restricted": 0,
        "modules": ("项目管理系统", "工时系统", "采购门户", "运营中台", "客户台账"),
        "err_prefix": "PO-ERR",
        "roles": ("项目/运营业务人员", "项目系统管理员", "项目主管"),
    },
    {
        "key": "AD", "name": "行政", "domain": "administration",
        "internal": 0, "restricted": 0,
        "modules": ("OA 办公系统", "会议室系统", "固定资产台账", "用车平台", "印章系统"),
        "err_prefix": "AD-ERR",
        "roles": ("行政业务人员", "OA/资产管理员", "行政主管"),
    },
)

# 种子文档类型（document_type）：按内容分类（category）语义确定
CATEGORY_DOCUMENT_TYPE: dict[str, str] = {
    "系统操作": "operation_guide",
    "报错排查": "troubleshooting",
    "参数配置": "configuration_guide",
    "业务流程": "process_flow",
    "报表查询": "report_guide",
    "月末/年末结账": "closing_procedure",
    "特殊边界业务": "edge_case_handbook",
}

# 种子问题类型（question_type）：与既有评测问题模板语义一致
CATEGORY_QUESTION_TYPE: dict[str, str] = {
    "系统操作": "how_to",
    "报错排查": "troubleshooting",
    "参数配置": "configuration",
    "业务流程": "process",
    "报表查询": "report_query",
    "月末/年末结账": "closing",
    "特殊边界业务": "edge_case",
}

# 种子适用角色（applicable_role）的选择模板：角色槽位映射（0/1/2）
CATEGORY_ROLE_SLOT: dict[str, int] = {
    "系统操作": 0, "报错排查": 1, "参数配置": 1, "业务流程": 0,
    "报表查询": 0, "月末/年末结账": 2, "特殊边界业务": 2,
}

# 种子系统版本：无真实版本，统一给出明确含 seed 的合成占位
DEMO_SYSTEM_VERSION = "2026.1-seed"

# 每个部门 220 条：7 个内容分类的条数（合计 220，覆盖全部要求类别）。
CATEGORY_PLAN: tuple[tuple[str, int], ...] = (
    ("系统操作", 32),
    ("报错排查", 32),
    ("参数配置", 32),
    ("业务流程", 31),
    ("报表查询", 31),
    ("月末/年末结账", 31),
    ("特殊边界业务", 31),
)
CHUNKS_PER_DEPT = sum(n for _, n in CATEGORY_PLAN)  # 220

#: 敏感（internal/restricted）条目只从这些“含财务口径”的分类中产生。
_SENSITIVE_CATEGORIES = ("月末/年末结账", "报表查询", "特殊边界业务", "业务流程", "参数配置")

FINANCE_PROJECT = "finance-ledger"
FINANCE_DOMAIN = "finance"

# 通用子主题词表（内容分类内的子主题；序号变体生成不同条目）
_TOPICS: dict[str, tuple[str, ...]] = {
    "系统操作": ("登录与身份验证", "用户权限申请", "数据批量导入", "单据审核流转",
                 "系统菜单导航", "用户账号解锁", "历史数据查询", "定时任务执行"),
    "报错排查": ("连接超时", "单据无法保存", "权限不足提示", "审批流中断",
                 "数据不一致", "导出失败", "缓存未刷新", "服务不可用"),
    "参数配置": ("单据号规则", "审批阈值", "数据保留期", "导出上限",
                 "缓存刷新周期", "附件大小限制", "会话超时时间", "汇率/系数配置"),
    "业务流程": ("日常单据处理", "跨部门协作", "采购申请", "费用申请",
                 "入职转正", "合同签订", "物料领用", "设备借用"),
    "报表查询": ("经营日报", "部门月报", "明细账查询", "预算执行表",
                 "同期对比", "趋势分析", "清单导出", "汇总统计"),
    "月末/年末结账": ("月度结账", "年末结账", "结账前检查", "损益结转",
                      "凭证过账", "暂估冲销", "结账清单", "结账复核"),
    "特殊边界业务": ("跨年业务", "红冲处理", "并发冲突", "负值场景",
                     "金额为零", "时间倒挂", "越权回退", "数据补录"),
}


def _num_based(idx: int) -> tuple[int, int, int, int, int]:
    """由条目序号派生一组确定性数值（金额/天数/百分比等，纯合成占位）。"""
    days = 1 + idx % 30
    money = 5000 + (idx * 1733) % 900000
    ratio = 10 + (idx * 7) % 81
    hours = 1 + idx % 23
    limit = 10 + idx % 90
    return days, money, ratio, hours, limit


def _demo_note(classification: str) -> str:
    if classification == "public":
        return ""
    return ("（测试期种子知识：其中数值与口径均为合成占位，"
            "不代表任何真实公司、财务或经营数据。）")


def _err_code(dept: dict, idx: int) -> str:
    return f"{dept['err_prefix']}-{2000 + idx % 900}"


def _body_text(dept: dict, category: str, topic: str, idx: int,
               module: str, classification: str) -> str:
    days, money, ratio, hours, limit = _num_based(idx)
    if category == "系统操作":
        text = (
            f"在{module}中，{topic}的标准操作步骤：使用个人账号登录系统，进入"
            f"「{module}·{topic}」菜单，先按页面提示完成身份核验，再按业务需要选择动作。"
            f"处理结果可在「操作记录」中查询，系统保留最近 {days} 天的操作日志。"
            f"如遇页面加载缓慢，可先清空浏览器缓存后重试；操作过程请勿在他人终端代做。"
        )
    elif category == "报错排查":
        code = _err_code(dept, idx)
        text = (
            f"使用{module}时出现「{topic}」报错 {code}：首先检查网络连接与服务端状态，"
            f"确认服务不在维护窗口内；随后按错误码到「帮助中心·{code}」查询处理建议。"
            f"若为单据无法保存，请核对必填项与附件大小（上限 {limit}MB）；若为审批流"
            f"中断，请由流程管理员在「流程监控」中重启该实例。问题仍存在时，将报错截图"
            f"与操作时间提交给系统管理员，并保留上下文日志以便排查。"
        )
    elif category == "参数配置":
        text = (
            f"{module}的「{topic}」参数建议配置如下（合成占位）：取值范围按业务规模核定，"
            f"示例值为 {money} 以内、保留 {limit} 条历史、会话超时 {hours} 分钟。修改参数"
            f"需具备管理员权限，保存后按提示重启相关模块或等待缓存刷新（最长 {days} 秒）。"
            f"参数变更记录写入审计日志，修改前建议截图保存原值以便回退。"
        )
    elif category == "业务流程":
        text = (
            f"「{topic}」业务在{module}中的处理流程：第一步由发起人填写申请并上传附件，"
            f"第二步由部门主管在 {hours} 个工作小时内完成初审，第三步流转至对口岗位审核，"
            f"超过 {money} 元的事项需升级至更高权限审批，第四步办结归档。全程可在"
            f"「流程跟踪」中查看节点耗时与处理人；处理记录默认保留 {days} 天。"
        )
    elif category == "报表查询":
        text = (
            f"「{topic}」报表在{module}中通过「报表查询」生成：选择统计期间（按日/月/年）"
            f"与维度（部门、科目、项目）后点击查询，默认展示最近 {days} 天数据并支持导出"
            f"上限 {limit} 万行。结果页提供趋势列与同环比列，可下钻至单据明细。报表口径与"
            f"取数逻辑见数据字典；跨部门数据按权限范围展示，未授权维度不显示。"
        )
    elif category == "月末/年末结账":
        text = (
            f"{module}的「{topic}」处理（合成口径）：结账窗口期内先执行未过账单据检查，"
            f"再按科目顺序过账并核对差异，{ratio}% 以上分项须在系统内完成复核后方可关闭"
            f"账期。结账完成后生成结账清单，反结账需高级权限并在 {days} 天内申请。年末场景"
            f"额外包含损益结转与年初开账检查；所有结账动作均写入操作审计。"
        )
    else:  # 特殊边界业务
        text = (
            f"「{topic}」属于{module}中的边界业务：处理前先确认业务期间与权限范围，"
            f"对金额为 0、负值或跨期数据系统会给出提示并要求二次确认。涉及冲销或补录时，"
            f"须保留原始凭证号并在备注注明原因，相关操作需在 {hours} 个工作日内由复核岗"
            f"确认。如遇并发冲突，系统以后提交版本为准并提示冲突对象；补录数据计入当日汇总。"
        )
    return text + _demo_note(classification)


def _assign_classifications(dept: dict) -> list[dict]:
    """构造每部门的条目计划 [(cat, k, classification, seq), ...]，共 220 条。

    restricted / internal 条目落在 _SENSITIVE_CATEGORIES 内（保持条目生成顺序），
    restricted 排在 internal 之后（结账/边界等尾部）更贴近“更敏感”的直觉。
    """
    plan: list[dict] = []
    for cat, n in CATEGORY_PLAN:
        for k in range(n):
            plan.append({"cat": cat, "k": k})

    # 候选槽位 = 敏感分类的条目（按生成顺序）
    slots = [p for p in plan if p["cat"] in _SENSITIVE_CATEGORIES]
    total_sensitive = dept["internal"] + dept["restricted"]
    if total_sensitive > len(slots):
        raise AssertionError(f"{dept['name']} 敏感条数超出可放置槽位")

    # 从尾部倒序标记 restricted（最尾部 → restricted），再往前 internal。
    slot_iter = reversed(slots)
    for i in range(dept["restricted"]):
        next(slot_iter)["classification"] = "restricted"
    for i in range(dept["internal"]):
        next(slot_iter)["classification"] = "internal"
    for p in plan:
        p.setdefault("classification", "public")
    return plan


def _build_graph(dept: dict, doc: Document, cat: str, topic: str, module: str,
                 code: str, role: str, inherited: dict,
                 chunk_id: str) -> tuple[list[GraphNode], list[GraphEdge]]:
    """为该种子文档构建确定性图谱（节点+边），全部携带继承元数据快照。

    图内表达：department / module / submodule（功能区）/ role / article /
    process（流程节点）/ error_code，以及 belongs_to / applies_to /
    dependency / precondition / cause / solution 关系。所有描述均标注为种子合成。
    """
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    def _node(node_type: str, name: str, desc: str = "") -> str:
        node_id = f"{doc.document_id}:{node_type}:{name}"
        nodes.append(GraphNode(
            node_id=node_id, name=name, node_type=node_type,
            document_id=doc.document_id, description=desc,
            meta=dict(inherited),
        ))
        return node_id

    def _edge(src: str, tgt: str, pred: str, desc: str = "") -> None:
        edges.append(GraphEdge(
            edge_id=f"{doc.document_id}:e{len(edges) + 1:03d}",
            document_id=doc.document_id,
            source_node_id=src, target_node_id=tgt, predicate=pred,
            evidence_chunk_id=chunk_id, description=desc,
            meta=dict(inherited),
        ))

    did = doc.document_id
    dep_id = _node("department", dept["name"])
    mod_id = _node("module", module)
    sub_id = _node("submodule", cat)
    art_id = _node("article", doc.title, desc=f"{module}·{cat}·{topic} 知识条目（种子）")

    _edge(art_id, sub_id, "belongs_to", f"知识条目归属功能区 {cat}")
    _edge(sub_id, mod_id, "belongs_to", f"功能区 {cat} 归属模块 {module}")
    _edge(mod_id, dep_id, "belongs_to", f"模块 {module} 归属部门 {dept['name']}")
    if role:
        role_id = _node("role", role)
        _edge(art_id, role_id, "applies_to", f"知识条目适用于角色 {role}（种子）")

    if cat == "报错排查":
        err_id = _node("error_code", code, desc=f"{dept['name']}·{module}「{topic}」报错码（种子）")
        cause_id = _node("process", f"{code}·成因（种子）",
                         desc="网络连接或服务端维护等常见成因（种子）")
        sol_id = _node("process", f"{code}·处理建议（种子）",
                       desc=f"按帮助中心 {code} 指引排查，必要时提交管理员（种子）")
        _edge(err_id, cause_id, "cause", f"报错 {code} 的常见成因（种子）")
        _edge(err_id, sol_id, "solution", f"报错 {code} 的处理建议（种子）")
        _edge(err_id, mod_id, "belongs_to", f"报错码 {code} 归属模块 {module}")
    elif cat == "业务流程":
        steps = (f"{topic}·发起申请", f"{topic}·主管初审",
                 f"{topic}·对口岗位审核", f"{topic}·办结归档")
        pids = [_node("process", s, desc=f"{module}·{topic} 流程节点（种子）") for s in steps]
        _edge(pids[1], pids[0], "dependency", "初审依赖发起申请完成")
        _edge(pids[2], pids[1], "dependency", "对口岗位审核依赖初审完成")
        _edge(pids[3], pids[2], "dependency", "办结归档依赖复审完成")
        cond = _node("process", f"{topic}·申请资料与附件齐备（种子）")
        _edge(pids[0], cond, "precondition", "发起申请的前置条件（种子）")
    elif cat == "月末/年末结账":
        pids = [
            _node("process", f"{topic}·未过账单据检查", desc="结账窗口期前置检查（种子）"),
            _node("process", f"{topic}·按科目顺序过账", desc="过账与差异核对（种子）"),
            _node("process", f"{topic}·复核并关闭账期", desc="分项复核后关闭账期（种子）"),
        ]
        _edge(pids[1], pids[0], "dependency", "过账依赖未过账单据检查完成")
        _edge(pids[2], pids[1], "dependency", "关闭账期依赖过账完成")
        cond = _node("process", f"{topic}·分项复核比例达标（种子）")
        _edge(pids[2], cond, "precondition", "关闭账期的复核前置条件（种子）")
    elif cat == "特殊边界业务":
        pids = [
            _node("process", f"{topic}·边界业务受理确认（种子）",
                  desc="先确认业务期间与权限范围（种子）"),
            _node("process", f"{topic}·复核确认与归档（种子）",
                  desc="复核岗确认后归档（种子）"),
        ]
        _edge(pids[1], pids[0], "dependency", "复核确认依赖受理确认完成")
        cond = _node("process", f"{topic}·业务期间与权限范围确认（种子）")
        _edge(pids[0], cond, "precondition", "受理确认的前置条件（种子）")
    return nodes, edges


def _make_docs(dept: dict) -> list[Document]:
    plan = _assign_classifications(dept)
    docs: list[Document] = []
    roles = tuple(dept.get("roles") or ())
    # 旧部门标签 → 新契约（可审阅映射；见 models.LEGACY_DEPARTMENT_CONTRACT）
    contract = LEGACY_DEPARTMENT_CONTRACT[dept["name"]]
    owner_department = contract["owner_department"]
    business_category = contract["business_category"]
    applicable_departments = list(contract["applicable_departments"])
    for seq, item in enumerate(plan, start=1):
        cat, k, classification = item["cat"], item["k"], item["classification"]
        module = dept["modules"][seq % len(dept["modules"])]
        topic = _TOPICS[cat][k % len(_TOPICS[cat])]
        idx = seq - 1  # 0-based 全局序号（驱动数值派生）
        code = _err_code(dept, idx) if cat == "报错排查" else ""
        content = _body_text(dept, cat, topic, idx, module, classification)
        project_id = None if classification == "public" else FINANCE_PROJECT
        business_domain = dept["domain"] if classification == "public" else FINANCE_DOMAIN
        access_scope = derive_access_scope(classification, project_id)
        title = f"{dept['name']}·{cat}·{topic}·{module}·{seq:03d}"
        doc_id = f"{dept['key']}-{seq:04d}"
        slot = CATEGORY_ROLE_SLOT.get(cat, 0)
        role = roles[slot] if roles and slot < len(roles) else (roles[0] if roles else "")
        doc = Document(
            document_id=doc_id,
            title=title,
            version="1.0",
            status="active",
            classification=classification,
            project_id=project_id,
            business_domain=business_domain,
            department=dept["name"],   # 历史/兼容字段（保留旧标签）
            access_scope=access_scope,
            owner_department=owner_department,
            business_category=business_category,
            applicable_departments=list(applicable_departments),
            owner="",
            source=SEED_SOURCE,
            pages=1,
            created="2026-01-01T00:00:00+00:00",
            updated="2026-01-01T00:00:00+00:00",
            content=content,
            metadata={
                "category": cat, "topic": topic, "demo": True,
                "module": module,
                "submodule": cat,   # 功能区 = 内容分类标签
                "document_type": CATEGORY_DOCUMENT_TYPE[cat],
                "applicable_role": role,
                "question_type": CATEGORY_QUESTION_TYPE[cat],
                "system_version": DEMO_SYSTEM_VERSION,
                "error_code": code,
                # 适用范围层（授权无关；与列值一致，供 JSON 消费者直接读取）
                "owner_department": owner_department,
                "business_category": business_category,
                "applicable_departments": list(applicable_departments),
                "access_scope": access_scope,
                **SEED_PROVENANCE,
            },
        )
        inherited = inherit_metadata(doc)
        nodes, edges = _build_graph(
            dept, doc, cat, topic, module, code, role, inherited,
            chunk_id=f"{doc_id}:1:1",
        )
        doc.graph_nodes = nodes
        doc.graph_edges = edges
        docs.append(doc)
    return docs


def make_all_docs() -> list[Document]:
    docs: list[Document] = []
    for dept in DEPT_DEFS:
        docs.extend(_make_docs(dept))
    return docs


def build(path: str | Path, *, force: bool = False) -> dict:
    """构建（或覆盖）知识库文件；返回统计报告。"""
    path = Path(path)
    if force and path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    store = KnowledgeStore(path)
    try:
        all_docs = make_all_docs()
        total_chunks = store.ingest_many(all_docs)
    finally:
        store.close()

    # 重新打开统计（文件库持久化后计数）。种子统计只计 source=SEED_SOURCE 的行，
    # 避免把同库中的资料工作台条目算进种子基线。
    s = KnowledgeStore(path)
    try:
        by_dept: dict[str, int] = {}
        by_class: dict[str, int] = {}
        for dept_name, classification, c in s.conn.execute(
            "SELECT department, classification, COUNT(*) FROM documents "
            "WHERE source = ? GROUP BY department, classification",
            (SEED_SOURCE,),
        ).fetchall():
            by_dept[dept_name] = by_dept.get(dept_name, 0) + c
            by_class[classification] = by_class.get(classification, 0) + c
        n_docs = s.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE source = ?",
            (SEED_SOURCE,)).fetchone()[0]
        n_chunks = s.conn.execute(
            "SELECT COUNT(*) FROM chunks c JOIN documents d "
            "ON d.document_id = c.document_id WHERE d.source = ?",
            (SEED_SOURCE,)).fetchone()[0]
        db_docs_total = s.conn.execute(
            "SELECT COUNT(*) FROM documents").fetchone()[0]
        integrity = s.conn.execute("PRAGMA integrity_check").fetchone()[0]
        n_graph_nodes = s.conn.execute(
            "SELECT COUNT(*) FROM graph_nodes g "
            "JOIN documents d ON d.document_id = g.document_id "
            "WHERE d.source = ?", (SEED_SOURCE,)).fetchone()[0]
        n_graph_edges = s.conn.execute(
            "SELECT COUNT(*) FROM graph_edges e "
            "JOIN documents d ON d.document_id = e.document_id "
            "WHERE d.source = ?", (SEED_SOURCE,)).fetchone()[0]
        # 种子 chunk 必须具备非空继承元数据（种子 source 行不允许空白快照）
        demo_missing_meta = s.conn.execute(
            "SELECT COUNT(*) FROM chunks c JOIN documents d "
            "ON d.document_id = c.document_id "
            "WHERE d.source = ? AND c.metadata_json = '{}'", (SEED_SOURCE,)
        ).fetchone()[0]
        # 出处完整性：documents 行必须带齐 PROVENANCE_FIELDS（键存在且非空）
        missing_provenance = 0
        for (mj,) in s.conn.execute(
            "SELECT metadata_json FROM documents WHERE source = ?", (SEED_SOURCE,)
        ).fetchall():
            meta = json.loads(mj or "{}")
            if any(
                k not in meta or meta[k] is None
                or (isinstance(meta[k], str) and not meta[k].strip())
                for k in PROVENANCE_FIELDS
            ):
                missing_provenance += 1
        # 内容适用范围契约完整性（documents 列 + chunk/图谱快照都要写新字段）
        contract_keys = ["access_scope", *APPLICABILITY_FIELDS]

        def _contract_missing(meta: dict) -> bool:
            for k in contract_keys:
                v = meta.get(k)
                if v is None or v == "" or v == []:
                    return True
            return False

        docs_missing_contract = 0
        for (mj,) in s.conn.execute(
            "SELECT metadata_json FROM documents WHERE source = ?", (SEED_SOURCE,)
        ).fetchall():
            if _contract_missing(json.loads(mj or "{}")):
                docs_missing_contract += 1
        chunks_missing_contract = 0
        for (mj,) in s.conn.execute(
            "SELECT c.metadata_json FROM chunks c JOIN documents d "
            "ON d.document_id = c.document_id WHERE d.source = ?", (SEED_SOURCE,)
        ).fetchall():
            if _contract_missing(json.loads(mj or "{}")):
                chunks_missing_contract += 1
        # 图谱快照（节点/边）继承同一契约
        graph_missing_contract = 0
        for table in ("graph_nodes", "graph_edges"):
            for (mj,) in s.conn.execute(
                f"SELECT g.meta_json FROM {table} g JOIN documents d "
                "ON d.document_id = g.document_id WHERE d.source = ?", (SEED_SOURCE,)
            ).fetchall():
                if _contract_missing(json.loads(mj or "{}")):
                    graph_missing_contract += 1
        # 适用范围分布（按新契约列，不按旧 department）
        by_owner_department = dict(s.conn.execute(
            "SELECT owner_department, COUNT(*) FROM documents WHERE source = ? "
            "GROUP BY owner_department ORDER BY owner_department", (SEED_SOURCE,)
        ).fetchall())
        by_business_category = dict(s.conn.execute(
            "SELECT business_category, COUNT(*) FROM documents WHERE source = ? "
            "GROUP BY business_category ORDER BY business_category", (SEED_SOURCE,)
        ).fetchall())
        applicable_index_rows = s.conn.execute(
            "SELECT COUNT(*) FROM document_applicable_departments a "
            "JOIN documents d ON d.document_id = a.document_id "
            "WHERE d.source = ?", (SEED_SOURCE,)).fetchone()[0]
        multi_applicable = s.conn.execute(
            "SELECT COUNT(*) FROM (SELECT a.document_id, COUNT(*) AS n "
            "FROM document_applicable_departments a JOIN documents d "
            "ON d.document_id = a.document_id WHERE d.source = ? "
            "GROUP BY a.document_id HAVING n > 1)", (SEED_SOURCE,)).fetchone()[0]
        by_access_scope = dict(s.conn.execute(
            "SELECT access_scope, COUNT(*) FROM documents WHERE source = ? "
            "GROUP BY access_scope ORDER BY access_scope", (SEED_SOURCE,)
        ).fetchall())
    finally:
        s.close()

    expected_total = CHUNKS_PER_DEPT * len(DEPT_DEFS)
    return {
        "database": str(path.resolve()),
        "source": SEED_SOURCE,
        "seed_label": SEED_LABEL,
        "departments_expected_each": CHUNKS_PER_DEPT,
        "total_docs_expected": expected_total,
        "counts": {"documents": n_docs, "chunks": n_chunks},
        "database_documents_total": db_docs_total,
        "by_department": by_dept,
        "by_classification": by_class,
        "chunks_created_total": total_chunks,
        "integrity_check": integrity,
        "graph": {"nodes": n_graph_nodes, "edges": n_graph_edges},
        "demo_chunks_missing_metadata": demo_missing_meta,
        "seed_chunks_missing_provenance": missing_provenance,
        "provenance": {
            "origin": SEED_ORIGIN,
            "license": SEED_LICENSE,
            "is_real_company_data": False,
            "seed_label": SEED_LABEL,
            "fields": list(PROVENANCE_FIELDS),
        },
        "content_contract": {
            "authorization_fields": ["classification", "access_scope", "project_id",
                                     "business_domain", "status"],
            "applicability_fields": list(APPLICABILITY_FIELDS),
            "contract_fields": contract_keys,
            "applicable_department_index_table": "document_applicable_departments",
            "by_owner_department": by_owner_department,
            "by_business_category": by_business_category,
            "by_access_scope": by_access_scope,
            "applicable_department_index_rows": applicable_index_rows,
            "documents_with_multi_applicable_departments": multi_applicable,
            "docs_missing_contract_fields": docs_missing_contract,
            "chunks_missing_contract_fields": chunks_missing_contract,
            "graph_rows_missing_contract_fields": graph_missing_contract,
            "legacy_department_mapping": {
                k: {"owner_department": v["owner_department"],
                    "business_category": v["business_category"],
                    "applicable_departments": list(v["applicable_departments"])}
                for k, v in LEGACY_DEPARTMENT_CONTRACT.items()
            },
            "finance_compliance_rule": {
                **FINANCE_COMPLIANCE_RULE,
                "applicable_departments": list(
                    FINANCE_COMPLIANCE_RULE["applicable_departments"]),
            },
        },
    }


def make_eval_questions() -> list[dict]:
    """从确定性语料构造种子评测问题（黄金文档 = 来源 document_id）。

    每部门取 ~8 条样本（均匀分布），覆盖 7 个内容分类；gold.contains 取正文
    前段可检索子串，便于 hit@k / MRR 离线评测（knowledge.eval）。
    """
    questions: list[dict] = []
    category_question = {
        "系统操作": "在{dep}中，{topic}的操作步骤是什么？",
        "报错排查": "{dep}遇到“{topic}”报错时应如何排查？",
        "参数配置": "{dep}的“{topic}”参数应如何配置？",
        "业务流程": "{dep}中“{topic}”的处理流程是什么？",
        "报表查询": "{dep}的“{topic}”报表如何查询与解读？",
        "月末/年末结账": "{dep}的“{topic}”如何处理？",
        "特殊边界业务": "{dep}遇到“{topic}”这类边界业务如何处理？",
    }
    for dept in DEPT_DEFS:
        docs = _make_docs(dept)
        step = max(1, len(docs) // 8)
        for doc in docs[::step][:8]:
            category = doc.metadata.get("category", "")
            topic = doc.metadata.get("topic", "")
            template = category_question.get(category)
            if not template:
                continue
            q = template.format(dep=dept["name"], topic=topic)
            text = doc.content.replace("\n", " ").strip()
            questions.append({
                "question": q,
                "gold": {"document_id": doc.document_id, "contains": text[:18]},
                "meta": {
                    "department": dept["name"],           # 历史/兼容字段
                    "classification": doc.classification,
                    "category": category,
                    # 适用范围契约（授权无关）
                    "owner_department": doc.owner_department,
                    "business_category": doc.business_category,
                    "applicable_departments": list(doc.applicable_departments),
                    "access_scope": doc.access_scope,
                    **SEED_PROVENANCE,
                },
            })
    return questions


def write_eval_questions(path: str | Path) -> int:
    """写出种子评测问题文件，返回条目数。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = make_eval_questions()
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Knowledge Lantern 测试期种子知识库构建（合成/整理语料，非真实公司数据）")
    parser.add_argument("--db", default=str(_PROJECT_ROOT / "data" / "knowledge.db"))
    parser.add_argument("--force", action="store_true",
                        help="先删除目标库文件再重建（破坏性：会丢失库内非种子条目，"
                             "请先备份；默认不加 = 增量覆盖，ID/内容一致）")
    parser.add_argument(
        "--report",
        default=str(_PROJECT_ROOT / "data" / "knowledge_import_report.json"),
    )
    parser.add_argument(
        "--eval",
        default=str(_PROJECT_ROOT / "data" / "knowledge_demo" / "eval_questions.json"),
        help="种子评测问题输出路径（--no-eval 关闭）",
    )
    parser.add_argument("--no-eval", action="store_true", help="不生成评测问题")
    args = parser.parse_args(argv)

    report = build(args.db, force=args.force)
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.no_eval:
        n = write_eval_questions(args.eval)
        report["eval_questions"] = n
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
