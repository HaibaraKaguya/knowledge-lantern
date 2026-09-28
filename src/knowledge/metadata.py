"""文档元数据 JSON 的加载与校验 + 实体/关系的构造.

元数据 schema（最小必需字段）:
    document_id, title, version, status（active/draft/archived）
可选: classification（public/internal/restricted）, access_scope, project_id,
      business_domain, department（历史/兼容）, owner, source, pages, created,
      updated, owner_department, business_category, applicable_departments（列表）,
      entities: [{name, type, description}],
      relations: [{subject, predicate, object,
                   evidence: {chunk_ids, page, quote}}]

契约分层（见 models.py 模块头）：
  授权层   classification / access_scope / project_id / business_domain / status
  适用范围 applicable_departments（多选列表）/ owner_department / business_category
  `department` 为历史字段；新数据必须同时写适用范围新字段。
"""

from __future__ import annotations
import json
from pathlib import Path
from .models import (
    STATUSES, CLASSIFICATIONS, DEFAULT_PROVENANCE, Document, Entity, Relation,
    PROVENANCE_FIELDS, SEED_ORIGIN, SEED_PROVENANCE,
    BUSINESS_CATEGORIES, DEPARTMENT_KEYS, LEGACY_DEPARTMENT_CONTRACT,
    REAL_MATERIAL_ORIGINS, REAL_MATERIAL_REQUIRED_FIELDS,
    derive_access_scope, normalize_applicable_departments,
)


class MetadataError(ValueError):
    """元数据缺失或非法。"""


#: META_FIELDS 中当文档没有真实值时使用的"通用占位值"（文本值均明确标注为占位，
#: 绝不伪造真实公司信息；种子条目的真实取值见 SEED_PROVENANCE）。
#: 键集合 = META_FIELDS − AUTH_FIELDS − {'department','demo'}（即纯描述字段）。
#: 数值/内部字段（classification/…）占位见 inherit_metadata：无真实值时全部取
#: documents 列默认（public/None/'research'/active），并设置 demo 标记。
_PLACEHOLDER_DEFAULTS: dict[str, str] = {
    "module": "未标注模块（通用占位）",
    "submodule": "未标注子模块（通用占位）",
    "document_type": "generic",
    "applicable_role": "未标注角色（通用占位）",
    "question_type": "general",
    "system_version": "未标注版本（通用占位）",
    "error_code": "",
}


def _fill_descriptive_defaults(meta: dict) -> bool:
    """就地补齐 META_FIELDS 中的描述性占位值；返回是否动用过占位值。

    inherit_metadata 与 finalize_meta 共用的唯一实现（避免两处规则漂移）。
    """
    used_placeholder = False
    for key, default in _PLACEHOLDER_DEFAULTS.items():
        value = meta.get(key)
        if value is None or (isinstance(value, str) and not str(value).strip()):
            meta[key] = default
            if key != "error_code":   # 空报错码是合法空值，不算缺省占位
                used_placeholder = True
    return used_placeholder


def _resolve_provenance(base: dict) -> dict:
    """补齐 PROVENANCE_FIELDS：种子条目用固定种子出处，其余用未知缺省。

    只补“缺失”的键；已有值（含 is_real_company_data=False）原样保留，绝不
    把用户提交/未知来源的内容伪造成真实公司数据。
    """
    is_seed = base.get("origin") == SEED_ORIGIN
    defaults = SEED_PROVENANCE if is_seed else DEFAULT_PROVENANCE
    out: dict = {}
    for key in PROVENANCE_FIELDS:
        value = base.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            value = defaults[key]
        out[key] = value
    return out


def _resolve_applicability(base: dict, *, department: str = "",
                           owner_department: str = "",
                           business_category: str = "",
                           applicable_departments=None) -> dict:
    """解析内容适用范围（授权无关）：显式值 > metadata 键 > 旧部门标签映射。

    无任何依据时保持为空（[]/''），绝不臆造归属；旧 `department` 标签经
    LEGACY_DEPARTMENT_CONTRACT 映射，仅为兼容既有种子数据。
    """
    legacy = LEGACY_DEPARTMENT_CONTRACT.get(department or "", {})

    def _pick(explicit, meta_key, legacy_key, default):
        if explicit is not None and explicit != "" and explicit != []:
            return explicit
        value = base.get(meta_key)
        if value is not None and value != "" and value != []:
            return value
        return legacy.get(legacy_key, default)

    return {
        "applicable_departments": normalize_applicable_departments(
            _pick(applicable_departments, "applicable_departments",
                  "applicable_departments", [])),
        "owner_department": str(
            _pick(owner_department, "owner_department",
                  "owner_department", "") or ""),
        "business_category": str(
            _pick(business_category, "business_category",
                  "business_category", "") or ""),
    }


#: 旧部门标签 → 新契约映射（从 models 导入，供 metadata 层兼容解析）。
#: 见 models.LEGACY_DEPARTMENT_CONTRACT 与 FINANCE_COMPLIANCE_RULE。


def inherit_metadata(doc: Document) -> dict:
    """把文档级元数据解析为 chunk/图谱行继承用的完整快照（含 META_FIELDS）。

    取值优先级（描述性字段）：Document.metadata 里已声明的真实值 > 通用占位。
    授权字段（classification/access_scope/project_id/business_domain/status）以
    Document 字段（documents 表单一事实源）为准覆盖，绝不从 metadata 反推。
    内容适用范围（applicable_departments/owner_department/business_category）由
    Document 字段/metadata 显式值优先，缺省时回退到旧 department 标签映射。

    任何缺省字段都会被补成清楚标注为演示的通用值，并把顶层 `demo` 标记为 True；
    这些占位值只用于非演示（如用户提交资料）缺省时的可解释展示，不伪造公司数据。
    注意：本快照只用于描述性展示与离线检查；授权判定永远以 documents 表当前列为准。
    """
    doc_demo = bool(doc.metadata.get("demo")) if isinstance(doc.metadata, dict) else False
    base = dict(doc.metadata) if isinstance(doc.metadata, dict) else {}

    meta: dict[str, object] = {}
    # 描述性字段：文档级 metadata 优先，缺省者由 _fill_descriptive_defaults 填占位。
    for key in _PLACEHOLDER_DEFAULTS:
        value = base.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            meta[key] = value
    missing_demo_default = _fill_descriptive_defaults(meta)

    meta.update({
        "department": doc.department or "未标注部门（通用占位）",
        "classification": doc.classification,
        "access_scope": doc.access_scope or derive_access_scope(
            doc.classification, doc.project_id),
        "project_id": doc.project_id,
        "business_domain": doc.business_domain or "research",
        "status": doc.status,
    })
    # 内容适用范围（授权无关）：显式优先，缺省回退旧 department 标签映射。
    meta.update(_resolve_applicability(
        base,
        department=doc.department,
        owner_department=doc.owner_department,
        business_category=doc.business_category,
        applicable_departments=doc.applicable_departments or None,
    ))
    # 保留非必需描述字段（如 category/topic），其余原始 metadata 键保留。
    for k, v in base.items():
        if k not in meta:
            meta[k] = v
    # demo 标记：原文档是演示条目，或本次动用了通用占位值。
    meta["demo"] = bool(doc_demo or missing_demo_default)
    # 数据出处：种子条目写入固定 provenance；其余按缺省（来源未知）。
    meta.update(_resolve_provenance(base))
    return meta


def load_metadata(path) -> dict:
    """读取并校验 metadata.json，返回 dict。校验失败抛 MetadataError。"""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MetadataError(f"元数据文件不存在: {p}")
    except json.JSONDecodeError as e:
        raise MetadataError(f"元数据不是合法 JSON: {p} ({e})")

    if not isinstance(data, dict):
        raise MetadataError(f"元数据顶层必须是对象: {p}")

    for req in ("document_id", "title", "version"):
        if not data.get(req):
            raise MetadataError(f"缺少必填字段 '{req}': {p}")

    status = data.get("status", "active")
    if status not in STATUSES:
        raise MetadataError(f"非法 status '{status}'（可选: {STATUSES}）: {p}")

    classification = data.get("classification", "public")
    if classification not in CLASSIFICATIONS:
        raise MetadataError(
            f"非法 classification '{classification}'（可选: {CLASSIFICATIONS}）: {p}"
        )
    return data


def validate_ingest_metadata(meta: dict, *, path=None) -> dict:
    """校验一份入库元数据；真实资料（user_submitted / controlled_import）执行强制契约。

    - 真实资料：REAL_MATERIAL_REQUIRED_FIELDS 必须齐备且非空，classification /
      access_scope / applicable_departments / business_category / owner_department
      还必须取值合法（列表、白名单键）；否则抛 MetadataError。
    - 种子/未知来源：只做基础字段与取值合法性校验，不强制适用范围字段。
    返回原 dict（便于链式使用）。
    """
    where = f": {path}" if path else ""
    origin = meta.get("origin")
    if meta.get("is_real_company_data") is True and origin not in REAL_MATERIAL_ORIGINS:
        raise MetadataError(
            "声称 is_real_company_data=True 的资料必须声明受控来源 origin ∈ "
            f"{REAL_MATERIAL_ORIGINS}（不得用种子/未知来源冒充真实公司数据）{where}")
    if origin in REAL_MATERIAL_ORIGINS:
        missing = [
            k for k in REAL_MATERIAL_REQUIRED_FIELDS
            if k not in meta or meta[k] is None
            or (isinstance(meta[k], str) and not meta[k].strip())
            or (k == "applicable_departments" and not meta[k])
        ]
        if missing:
            raise MetadataError(
                f"真实资料（origin={origin}）缺少必填字段 {missing}{where}")
        if not isinstance(meta.get("applicable_departments"), list):
            raise MetadataError(
                f"applicable_departments 必须是列表（可多选）{where}")
    # 取值范围合法性（所有来源一旦提供就校验）
    if meta.get("business_category"):
        if meta["business_category"] not in BUSINESS_CATEGORIES:
            raise MetadataError(
                f"非法 business_category '{meta['business_category']}'"
                f"（可选: {BUSINESS_CATEGORIES}）{where}")
    if meta.get("owner_department"):
        if meta["owner_department"] not in DEPARTMENT_KEYS:
            raise MetadataError(
                f"非法 owner_department '{meta['owner_department']}'"
                f"（可选: {DEPARTMENT_KEYS}）{where}")
    for key in normalize_applicable_departments(meta.get("applicable_departments")):
        if key not in DEPARTMENT_KEYS:
            raise MetadataError(
                f"非法 applicable_departments 项 '{key}'（可选: {DEPARTMENT_KEYS}）{where}")
    return meta


def finalize_meta(meta: dict, *, department: str = "", classification: str = "public",
                  access_scope: str = "", project_id=None, business_domain: str = "",
                  status: str = "active", applicable_departments=None,
                  owner_department: str = "", business_category: str = "") -> dict:
    """把（可能不完整的）描述元数据补齐为完整可展示形状。

    供检索结果/图谱输出使用：以 chunk/图行快照为底，documents 行当前列为准
    覆盖授权字段；描述字段缺省时填通用占位（与 inherit_metadata 同规则）。
    内容适用范围同样以 documents 当前列为准覆盖。
    授权判定不使用本函数结果（一律 JOIN documents 当前列）。
    """
    out = dict(meta or {})
    out["department"] = department or out.get("department") or "未标注部门（通用占位）"
    out["classification"] = classification
    out["access_scope"] = access_scope or out.get("access_scope") or derive_access_scope(
        classification, project_id)
    out["project_id"] = project_id
    out["business_domain"] = business_domain or out.get("business_domain") or "research"
    out["status"] = status
    _fill_descriptive_defaults(out)
    # 内容适用范围：显式/文档列优先，缺省回退快照/旧部门标签映射。
    out.update(_resolve_applicability(
        out,
        department=out["department"],
        owner_department=owner_department,
        business_category=business_category,
        applicable_departments=applicable_departments,
    ))
    out["demo"] = bool(out.get("demo"))
    out.update(_resolve_provenance(out))
    return out


def _entities_from(data: dict) -> list:
    out = []
    for ent in data.get("entities", []) or []:
        out.append(Entity(
            name=ent["name"],
            type=ent.get("type", ""),
            description=ent.get("description", ""),
        ))
    return out


def _relations_from(data: dict) -> list:
    out = []
    for rel in data.get("relations", []) or []:
        ev = rel.get("evidence") or {}
        out.append(Relation(
            subject=rel["subject"],
            predicate=rel["predicate"],
            object=rel["object"],
            evidence_chunk_ids=list(ev.get("chunk_ids") or []),
            evidence_page=int(ev.get("page", 1)),
            evidence_quote=ev.get("quote", ""),
        ))
    return out


def load_document_from_files(doc_dir) -> Document:
    """从目录加载一份文档: <doc_dir>/metadata.json + <doc_dir>/content.md。

    真实资料（origin=user_submitted/controlled_import）会执行入库契约校验
    （validate_ingest_metadata），缺少新契约必填字段时抛 MetadataError。
    """
    d = Path(doc_dir)
    meta = load_metadata(d / "metadata.json")
    validate_ingest_metadata(meta, path=d / "metadata.json")
    content = (d / "content.md").read_text(encoding="utf-8")

    return Document(
        document_id=str(meta["document_id"]),
        title=str(meta["title"]),
        version=str(meta["version"]),
        status=str(meta.get("status", "active")),
        classification=str(meta.get("classification", "public")),
        access_scope=str(meta.get("access_scope", "")),
        project_id=meta.get("project_id"),
        business_domain=str(meta.get("business_domain", "research")),
        department=str(meta.get("department", "")),
        owner_department=str(meta.get("owner_department", "")),
        business_category=str(meta.get("business_category", "")),
        applicable_departments=normalize_applicable_departments(
            meta.get("applicable_departments")),
        owner=str(meta.get("owner", "")),
        source=str(meta.get("source", "")),
        pages=int(meta.get("pages", 1)),
        created=str(meta.get("created", "")),
        updated=str(meta.get("updated", "")),
        content=content,
        entities=_entities_from(meta),
        relations=_relations_from(meta),
        metadata=meta,
    )
