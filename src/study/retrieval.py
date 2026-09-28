"""Hybrid FTS, optional vectors, one-hop graph expansion, and weighted RRF."""
from __future__ import annotations
import json
from collections import defaultdict
from .models import RetrievalCandidate

def rrf_group(branches: dict[str, list[str]], weights: dict[str, float] | None = None, k: int = 60) -> list[RetrievalCandidate]:
    weights = {"bm25": 1.0, "vector": 1.0, "graph": .6, **(weights or {})}
    scores, ranks = defaultdict(float), defaultdict(dict)
    for branch, ids in branches.items():
        if weights.get(branch, 0) <= 0:
            continue
        for rank, record_id in enumerate(dict.fromkeys(ids), 1):
            scores[record_id] += weights[branch] / (k + rank)
            ranks[record_id][branch] = rank
    return [RetrievalCandidate(record_id, score, dict(ranks[record_id])) for record_id, score in sorted(scores.items(), key=lambda row: (-row[1], row[0]))]

class StudyRetriever:
    """The optional vector provider returns already ranked record IDs; absent means disabled."""
    def __init__(self, store, vector_provider=None):
        self.store, self.vector_provider = store, vector_provider
    def search(self, query: str, limit: int = 5) -> list[RetrievalCandidate]:
        conn = self.store.conn
        terms = " ".join(part for part in query.replace("'", " ").split() if part)
        bm25 = [row[0] for row in conn.execute("SELECT record_id FROM study_fts WHERE study_fts MATCH ? ORDER BY bm25(study_fts) LIMIT 40", (terms,))] if terms else []
        concept_ids = [row[0] for row in conn.execute("SELECT concept_id, name, aliases_json FROM study_concepts") if row[1] in query or any(alias in query for alias in json.loads(row[2]))]
        graph = []
        if concept_ids:
            marks = ",".join("?" * len(concept_ids))
            sql = f"SELECT DISTINCT l.record_id FROM study_record_concepts l WHERE l.concept_id IN ({marks}) OR l.concept_id IN (SELECT to_concept_id FROM study_concept_edges WHERE from_concept_id IN ({marks}))"
            graph = [row[0] for row in conn.execute(sql, concept_ids + concept_ids)]
        vector = list(self.vector_provider(query) or []) if self.vector_provider else []
        return [RetrievalCandidate(candidate.record_id, candidate.score, candidate.branch_ranks, {branch: "matched" for branch in candidate.branch_ranks}) for candidate in rrf_group({"bm25": bm25, "vector": vector, "graph": graph})[:limit]]
