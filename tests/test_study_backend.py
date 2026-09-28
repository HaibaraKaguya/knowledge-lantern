from src.study import StudyStore, PhotoRecord, OCRText, Concept, RecordConceptLink, ConceptEdge, StudyRetriever, GapService
from src.study.api import create_study_router

def seed():
    store = StudyStore()
    store.add_record(PhotoRecord("r1", "images/r1.jpg", "h1", "2026-09-04", mastery="weak"))
    store.add_record(PhotoRecord("r2", "images/r2.jpg", "h2", "2026-09-03", mastery="mastered"))
    store.add_ocr(OCRText("r1", "二次函数 最大值 配方法")); store.add_ocr(OCRText("r2", "抛物线 顶点"))
    store.add_concept(Concept("quad", "二次函数", aliases=("抛物线",))); store.add_concept(Concept("complete", "配方法"))
    store.link(RecordConceptLink("r1", "quad", error_type="method")); store.link(RecordConceptLink("r2", "complete")); store.add_edge(ConceptEdge("quad", "complete"))
    return store
def test_media_metadata_and_fts_not_blob():
    store = seed(); assert tuple(store.conn.execute("SELECT image_path,image_hash FROM study_records WHERE record_id='r1'").fetchone()) == ("images/r1.jpg", "h1"); assert store.conn.execute("SELECT record_id FROM study_fts WHERE study_fts MATCH '最大值'").fetchone()[0] == "r1"
def test_graph_and_rrf_group_to_unique_records():
    results = StudyRetriever(seed(), vector_provider=lambda query: ["r1", "r2"]).search("二次函数 最大值")
    assert results[0].record_id == "r1"; assert len({item.record_id for item in results}) == len(results); assert {"bm25", "vector", "graph"} <= set(results[0].branch_ranks)
def test_gap_is_evidence_only_and_one_hop():
    gaps = GapService(seed()).gaps_for("quad"); assert gaps[0].concept_id == "quad" and gaps[0].weak_count == 1; assert all("saved records" in item.explanation for item in gaps)

def test_api_route_contract():
    router = create_study_router(seed())
    paths = {route.path: route.methods for route in router.routes}
    assert paths["/api/study/health"] == {"GET"}
    assert paths["/api/study/search"] == {"GET"}

if __name__ == "__main__":
    tests = [test_media_metadata_and_fts_not_blob, test_graph_and_rrf_group_to_unique_records, test_gap_is_evidence_only_and_one_hop, test_api_route_contract]
    for test in tests:
        test()
    print(f"{len(tests)} study backend tests passed")
