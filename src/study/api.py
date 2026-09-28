"""Minimal FastAPI boundary for the local study store.

The router is deliberately factory-based: a host app supplies its durable store,
while tests and demos receive an isolated in-memory store.
"""
from __future__ import annotations

try:  # Production hosts use FastAPI; the core remains importable without extras.
    from fastapi import APIRouter, Query
except ModuleNotFoundError:  # pragma: no cover - exercised by direct stdlib test run
    class _Route:
        def __init__(self, path, methods): self.path, self.methods = path, methods
    class APIRouter:  # minimal contract-only fallback, not an HTTP server
        def __init__(self, prefix="", tags=None): self.prefix, self.tags, self.routes = prefix, tags or [], []
        def get(self, path):
            def register(function):
                self.routes.append(_Route(self.prefix + path, {"GET"}))
                return function
            return register
    def Query(default, **_kwargs): return default
from .retrieval import StudyRetriever
from .store import StudyStore

def create_study_router(store: StudyStore | None = None) -> APIRouter:
    store = store or StudyStore()
    router = APIRouter(prefix="/api/study", tags=["study"])

    @router.get("/health")
    def health():
        return {"status": "ok", "storage": "sqlite"}

    @router.get("/search")
    def search(q: str = Query(..., min_length=1), limit: int = Query(5, ge=1, le=20)):
        results = StudyRetriever(store).search(q, limit)
        return {"query": q, "results": [{"record_id": item.record_id, "score": item.score, "branches": item.branch_ranks, "evidence": item.evidence} for item in results]}

    return router
