"""企业知识检索地基 — 文档元数据、实体关系、结构切分、SQLite 检索、离线评测.

基础层只依赖标准库（sqlite3 / json / dataclasses），可离线复现。
可选的向量/图谱组件（vector_index / hybrid_retrieval / graph_retrieval /
graphrag_pipeline）依赖 faiss/numpy 等可选库；缺失时上层按"组件关闭"降级，
不影响 FTS 基线。
"""

from .models import (
    ACCESS_SCOPE_COMPANY_PUBLIC, APPLICABILITY_FIELDS, AUTH_FIELDS,
    BUSINESS_CATEGORIES, CHUNK_CONTRACT_FIELDS, CLASSIFICATIONS,
    DEFAULT_PROVENANCE, DEPARTMENT_KEYS, DEPARTMENT_REGISTRY,
    FINANCE_COMPLIANCE_RULE, GRAPH_NODE_TYPES, GRAPH_PREDICATES,
    LEGACY_DEPARTMENT_CONTRACT, META_FIELDS, ORIGIN_CONTROLLED_IMPORT,
    ORIGIN_USER_SUBMITTED, PROVENANCE_FIELDS, REAL_MATERIAL_ORIGINS,
    REAL_MATERIAL_REQUIRED_FIELDS, SEED_LABEL, SEED_LICENSE, SEED_ORIGIN,
    SEED_PROVENANCE,
    Chunk, Document, Entity, GraphEdge, GraphNode, Relation, SearchResult,
    derive_access_scope, normalize_applicable_departments,
)
from .metadata import (
    MetadataError,
    finalize_meta,
    inherit_metadata,
    load_document_from_files,
    load_metadata,
    validate_ingest_metadata,
)
from .chunking import chunk_document
from .store import KnowledgeStore, open_store
from .corpus import load_corpus, load_qa
from .eval import evaluate_retrieval, hit_at_k

__all__ = [
    "Document", "Chunk", "Entity", "Relation", "SearchResult",
    "GraphNode", "GraphEdge",
    "CLASSIFICATIONS", "GRAPH_NODE_TYPES", "GRAPH_PREDICATES", "META_FIELDS",
    "PROVENANCE_FIELDS", "SEED_ORIGIN", "SEED_LICENSE", "SEED_LABEL",
    "SEED_PROVENANCE", "DEFAULT_PROVENANCE",
    "APPLICABILITY_FIELDS", "AUTH_FIELDS", "CHUNK_CONTRACT_FIELDS",
    "DEPARTMENT_KEYS", "DEPARTMENT_REGISTRY", "BUSINESS_CATEGORIES",
    "LEGACY_DEPARTMENT_CONTRACT", "FINANCE_COMPLIANCE_RULE",
    "ACCESS_SCOPE_COMPANY_PUBLIC", "ORIGIN_USER_SUBMITTED",
    "ORIGIN_CONTROLLED_IMPORT", "REAL_MATERIAL_ORIGINS",
    "REAL_MATERIAL_REQUIRED_FIELDS",
    "derive_access_scope", "normalize_applicable_departments",
    "load_document_from_files", "load_metadata", "MetadataError",
    "inherit_metadata", "finalize_meta", "validate_ingest_metadata",
    "chunk_document", "KnowledgeStore", "open_store",
    "load_corpus", "load_qa", "evaluate_retrieval", "hit_at_k",
]
