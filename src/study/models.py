"""Stable contracts for image-centred learning records; no web framework required."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Mapping

@dataclass(frozen=True)
class PhotoRecord:
    record_id: str
    image_path: str
    image_hash: str
    created_at: str
    title: str = ""
    note: str = ""
    status: str = "active"
    mastery: str = "unknown"
    starred: bool = False
@dataclass(frozen=True)
class OCRText:
    record_id: str
    text: str
    formula_text: str = ""
    engine: str = ""
    confidence: float | None = None
@dataclass(frozen=True)
class Concept:
    concept_id: str
    name: str
    subject: str = "math"
    aliases: tuple[str, ...] = ()
    description: str = ""
@dataclass(frozen=True)
class RecordConceptLink:
    record_id: str
    concept_id: str
    confidence: float = 1.0
    source: str = "user"
    error_type: str = ""
@dataclass(frozen=True)
class ConceptEdge:
    from_concept_id: str
    to_concept_id: str
    relation: str = "prerequisite"
    weight: float = 1.0
@dataclass(frozen=True)
class RetrievalCandidate:
    record_id: str
    score: float
    branch_ranks: Mapping[str, int | None] = field(default_factory=dict)
    evidence: Mapping[str, str] = field(default_factory=dict)
@dataclass(frozen=True)
class KnowledgeGap:
    concept_id: str
    concept_name: str
    evidence_count: int
    weak_count: int
    explanation: str
