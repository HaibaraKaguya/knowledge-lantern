"""学习模块 OCR 接入测试 —— 全部使用 fake 流水线（无真实模型、无真实图片上传）。

覆盖：
* 路径穿越拒绝（``..`` / 绝对路径 / 符号链接逃逸 / 不存在文件）；
* mock / real 状态透传（mock 永不冒充 real）；
* 主记录与审计 sidecar 分离，SQLite 索引不含图片 blob 或 OCR 原文；
* schema 校验失败不伪造成功；流水线失败不伪造结果；
* 受控 API 契约与权限（422/403/404/200 语义）。

fake 运行器只返回内存中的记录/审计，绝不加载权重或解码图片；
schema 复核使用只读的 LearningOCR ``schema.py``（源项目缺失时相关用例跳过）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from src.study import PhotoRecord, StudyStore  # noqa: E402
from src.study.ocr_adapter import (  # noqa: E402
    LearningOCRConfig,
    OCRAdapter,
    OCRUnavailable,
    RunResult,
    load_schema_module,
)
from src.study.ocr_api import create_study_ocr_router  # noqa: E402
from src.study.ocr_store import (  # noqa: E402
    OCRPathDenied,
    OCRRecordNotFound,
    StudyOCRPaths,
    StudyOCRStore,
)

SOURCE_ROOT = LearningOCRConfig.from_env().root
SCHEMA_PRESENT = (SOURCE_ROOT / "schema.py").is_file()
requires_schema = pytest.mark.skipif(
    not SCHEMA_PRESENT, reason="需要只读的 LearningOCR schema.py 做独立复核")

# 一张 1x1 的合成 PNG（不是真实用户图片，只用于验证受控写入路径）。
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000154a24f5f0000000049454e44ae426082"
)


# ---------------------------------------------------------------------------
# fake 流水线（无模型、无图片解码）
# ---------------------------------------------------------------------------


def _record(record_id: str, image_path: str) -> dict:
    return {
        "id": record_id,
        "subject": "limits",
        "question": {
            "text": "求函数的最大值",
            "formulas": [{"latex": "f(x)=-x^2+4x+5", "meaning": "开口向下的二次函数"}],
            "requirements": ["求最大值"],
        },
        "knowledge_points": ["二次函数最值"],
        "user_note": {"text": None, "knowledge_gap": None},
        "retrieval_text": "limits 二次函数 顶点 最大值",
        "image": image_path,
    }


def _audit(record_id: str, image_path: str, *, mode: str, ocr_status: str,
           model_status: str) -> dict:
    provider = "mock-vlm" if model_status != "real" else "huggingface-local"
    return {
        "schema_version": "3.0.0",
        "kind": "audit",
        "record_id": record_id,
        "created_at": "2026-09-15T00:00:00+00:00",
        "source": {
            "image": image_path, "image_sha256": "a" * 64, "width": 320,
            "height": 480, "subject": "limits", "subject_source": "test",
        },
        "ocr": {
            "provider": "mock" if ocr_status != "ok" else "paddleocr",
            "engine": "mock-ocr" if ocr_status != "ok" else "paddleocr-v3",
            "model": "mock-ocr v0.1" if ocr_status != "ok" else "PP-OCRv5_mobile",
            "status": ocr_status, "language": "ch", "confidence": 0.5,
            "blocks": [{"id": "b0", "text": "求函数的最大值", "bbox": [0, 0, 10, 10],
                        "confidence": 0.5, "type": "text", "metadata": {}}],
            "warning": None, "error": None,
        },
        "model": {
            "requested_mode": "auto", "mode": mode, "provider": provider,
            "name": "mock-vlm" if model_status != "real" else "Qwen/Qwen2.5-VL-3B-Instruct",
            "status": model_status, "confidence": None,
            "used_image": model_status == "real",
            "mock_reason": None if model_status == "real" else "weights absent",
        },
        "extraction": {
            "formulas": [{"latex": "f(x)=-x^2+4x+5", "role": "given",
                          "meaning": "二次函数", "evidence": ["ocr:b0"],
                          "confidence": None}],
            "requirements": [{"operation": "compute", "method": None, "target": None,
                              "text": "求最大值", "evidence": ["ocr:b0"],
                              "confidence": None}],
            "knowledge_points": [{"name": "二次函数最值", "level": 3, "relation": "core",
                                  "evidence": ["ocr:b0"], "confidence": None}],
        },
        "cleanup": {"normalizations": [], "merges": [], "dropped": []},
        "validation": {"valid": True, "errors": [], "warnings": []},
    }


class FakePipelineRunner:
    """确定性 fake：只回放预置记录/审计，不加载任何模型、不解码图片。"""

    def __init__(self, record=None, audit=None, *, exit_code=0, raises=None,
                 real_available=False):
        self.record = record
        self.audit = audit
        self.exit_code = exit_code
        self.raises = raises
        self.real_available = real_available
        self.calls = 0

    def capabilities(self) -> dict:
        return {
            "available": True, "root": str(SOURCE_ROOT), "python": sys.executable,
            "cli": str(SOURCE_ROOT / "cli.py"), "mock_available": True,
            "real_available": self.real_available,
            "ocr": {"real_ready": self.real_available, "status": "unavailable"},
            "vlm": {"real_ready": self.real_available, "status": "unavailable"},
            "modes": ["mock", "auto"] + (["real"] if self.real_available else []),
            "error": None,
        }

    def run(self, request) -> RunResult:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return RunResult(exit_code=self.exit_code, record=self.record,
                         audit=self.audit, stdout="fake", stderr="fake failure")


def _adapter(tmp_path, record, audit, **kw) -> OCRAdapter:
    store = StudyOCRStore(tmp_path)
    cfg = LearningOCRConfig(root=SOURCE_ROOT, python=Path(sys.executable))
    return OCRAdapter(store, cfg=cfg, runner=FakePipelineRunner(record, audit, **kw))


def _write_upload(store: StudyOCRStore, name: str = "q1.png") -> str:
    """把一个合成 PNG 放进受控上传目录，返回相对引用。"""
    store.paths.ensure()
    (store.paths.uploads_dir / name).write_bytes(PNG_BYTES)
    return name


# ---------------------------------------------------------------------------
# 1) 路径穿越拒绝
# ---------------------------------------------------------------------------


def test_resolve_upload_rejects_traversal(tmp_path):
    paths = StudyOCRPaths(tmp_path)
    paths.ensure()
    (paths.uploads_dir / "ok.png").write_bytes(PNG_BYTES)
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"secret")

    assert paths.resolve_upload("ok.png").name == "ok.png"
    for bad in ("../secret.png", "sub/../../secret.png", str(outside),
                "/etc/passwd", "", "   ", "ok.png\x00.txt"):
        with pytest.raises(OCRPathDenied):
            paths.resolve_upload(bad)


def test_resolve_upload_rejects_symlink_escape(tmp_path):
    paths = StudyOCRPaths(tmp_path)
    paths.ensure()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"secret")
    link = paths.uploads_dir / "link.png"
    link.symlink_to(outside)
    with pytest.raises(OCRPathDenied):
        paths.resolve_upload("link.png")


def test_save_upload_sanitizes_filename(tmp_path):
    adapter = _adapter(tmp_path, None, None)
    result = adapter.save_upload("../../evil.png", PNG_BYTES)
    assert ".." not in result["reference"]
    assert adapter.store.paths.resolve_upload(result["reference"]).is_file()
    with pytest.raises(OCRPathDenied):
        adapter.save_upload("notes.txt", PNG_BYTES)
    with pytest.raises(OCRPathDenied):
        adapter.save_upload("q1.png", b"")


def test_submit_rejects_reference_outside_uploads(tmp_path):
    adapter = _adapter(tmp_path, None, None)
    with pytest.raises(OCRPathDenied):
        adapter.submit(image_reference="../../etc/passwd")
    with pytest.raises(OCRPathDenied):
        adapter.submit(image_reference="/etc/passwd")


# ---------------------------------------------------------------------------
# 2) mock / real 状态透传
# ---------------------------------------------------------------------------


@requires_schema
def test_submit_mock_status_passthrough(tmp_path):
    store = StudyOCRStore(tmp_path)
    ref = _write_upload(store)
    record = _record("q1", str(store.paths.uploads_dir / ref))
    audit = _audit("q1", str(store.paths.uploads_dir / ref), mode="mock",
                   ocr_status="mock", model_status="mock")
    adapter = _adapter(tmp_path, record, audit)

    result = adapter.submit(image_reference=ref, mode="auto")
    assert result["status"] == "completed"
    assert result["mock"] is True and result["real"] is False
    assert result["mode_effective"] == "mock"
    assert result["ocr_status"] == "mock"
    assert result["model_status"] == "mock"
    assert result["validation"]["valid"] is True


@requires_schema
def test_submit_real_status_passthrough(tmp_path):
    store = StudyOCRStore(tmp_path)
    ref = _write_upload(store, "q2.png")
    image = str(store.paths.uploads_dir / ref)
    adapter = _adapter(tmp_path, _record("q2", image),
                       _audit("q2", image, mode="real", ocr_status="ok",
                              model_status="real"))
    result = adapter.submit(image_reference=ref, mode="real")
    assert result["mock"] is False and result["real"] is True
    assert result["ocr_status"] == "ok" and result["model_status"] == "real"


@requires_schema
def test_mock_never_masquerades_as_real(tmp_path):
    """mode=mock + status=real 是伪造声明：schema 校验必须判失败。"""
    store = StudyOCRStore(tmp_path)
    ref = _write_upload(store, "q3.png")
    image = str(store.paths.uploads_dir / ref)
    audit = _audit("q3", image, mode="mock", ocr_status="mock", model_status="real")
    adapter = _adapter(tmp_path, _record("q3", image), audit)
    result = adapter.submit(image_reference=ref)
    assert result["status"] == "invalid"
    assert result["real"] is False
    assert result["validation"]["valid"] is False


# ---------------------------------------------------------------------------
# 3) 主记录 / 审计分离；索引不含 blob 或 OCR 原文
# ---------------------------------------------------------------------------


@requires_schema
def test_primary_and_audit_are_separate_files(tmp_path):
    store = StudyOCRStore(tmp_path)
    ref = _write_upload(store, "q4.png")
    image = str(store.paths.uploads_dir / ref)
    adapter = _adapter(tmp_path, _record("q4", image),
                       _audit("q4", image, mode="mock", ocr_status="mock",
                              model_status="mock"))
    created = adapter.submit(image_reference=ref)
    record_id = created["record_id"]

    primary = store.read_primary(record_id)
    audit = store.read_audit(record_id)
    assert audit["kind"] == "audit"
    assert "ocr" not in primary and "validation" not in primary
    assert store.paths.primary_path(record_id).name == f"{record_id}.json"
    assert store.paths.audit_path(record_id).name == f"{record_id}.audit.json"


@requires_schema
def test_index_holds_metadata_only(tmp_path):
    store = StudyOCRStore(tmp_path)
    ref = _write_upload(store, "q5.png")
    image = str(store.paths.uploads_dir / ref)
    adapter = _adapter(tmp_path, _record("q5", image),
                       _audit("q5", image, mode="mock", ocr_status="mock",
                              model_status="mock"))
    adapter.submit(image_reference=ref)

    columns = {row[1] for row in store.conn.execute("PRAGMA table_info(study_ocr_jobs)")}
    assert columns == {
        "record_id", "image_reference", "image_sha256", "status", "mode_requested",
        "mode_effective", "ocr_status", "model_status", "mock", "valid", "error",
        "created_at", "primary_path", "audit_path", "image_filename", "source_record_id",
    }
    assert not any("blob" in c or c == "text" for c in columns)
    raw = store.paths.index_db.read_bytes()
    assert "求函数的最大值".encode("utf-8") not in raw  # OCR 原文不进索引库
    assert PNG_BYTES[:8] not in raw                     # 图片二进制不进索引库


@requires_schema
def test_result_survives_new_adapter_instance(tmp_path):
    store = StudyOCRStore(tmp_path)
    ref = _write_upload(store, "q6.png")
    image = str(store.paths.uploads_dir / ref)
    adapter = _adapter(tmp_path, _record("q6", image),
                       _audit("q6", image, mode="mock", ocr_status="mock",
                              model_status="mock"))
    created = adapter.submit(image_reference=ref)
    record_id = created["record_id"]

    # 新实例 + 会失败的运行器：读取必须来自磁盘 sidecar，而非重跑。
    reopened = OCRAdapter(StudyOCRStore(tmp_path), cfg=adapter.cfg,
                          runner=FakePipelineRunner(None, None, exit_code=1))
    result = reopened.get_result(record_id)
    assert result["record"]["id"] == record_id
    assert result["audit"]["kind"] == "audit"


# ---------------------------------------------------------------------------
# 4) schema 失败 / 流水线失败都不伪造
# ---------------------------------------------------------------------------


@requires_schema
def test_schema_failure_is_reported_not_faked(tmp_path):
    store = StudyOCRStore(tmp_path)
    ref = _write_upload(store, "q7.png")
    image = str(store.paths.uploads_dir / ref)
    bad = _record("q7", image)
    bad["extra_field"] = "not in contract"  # 违反 v3 主记录契约
    adapter = _adapter(tmp_path, bad, _audit("q7", image, mode="mock",
                                             ocr_status="mock", model_status="mock"))
    result = adapter.submit(image_reference=ref)
    assert result["status"] == "invalid"
    assert result["validation"]["valid"] is False
    assert result["validation"]["errors"]
    assert result["mock"] is True and result["real"] is False


def test_pipeline_failure_is_not_faked(tmp_path):
    store = StudyOCRStore(tmp_path)
    ref = _write_upload(store)
    adapter = _adapter(tmp_path, None, None, exit_code=1)
    result = adapter.submit(image_reference=ref)
    assert result["status"] == "failed"
    assert result["record"] is None and result["audit"] is None
    assert result["validation"]["valid"] is False
    assert result["error"]
    assert adapter.store.get_index(result["record_id"])["status"] == "failed"


def test_pipeline_exception_is_recorded(tmp_path):
    store = StudyOCRStore(tmp_path)
    ref = _write_upload(store)
    adapter = _adapter(tmp_path, None, None, raises=RuntimeError("boom"))
    result = adapter.submit(image_reference=ref)
    assert result["status"] == "failed"
    assert "boom" in result["error"]


def test_missing_schema_module_fails_loudly(tmp_path):
    store = StudyOCRStore(tmp_path)
    ref = _write_upload(store)
    image = str(store.paths.uploads_dir / ref)
    cfg = LearningOCRConfig(root=tmp_path / "no-such-source", python=Path(sys.executable))
    adapter = OCRAdapter(store, cfg=cfg, runner=FakePipelineRunner(
        _record("q8", image), _audit("q8", image, mode="mock", ocr_status="mock",
                                     model_status="mock")))
    with pytest.raises(OCRUnavailable):
        load_schema_module(cfg.root)
    result = adapter.submit(image_reference=ref)
    assert result["status"] == "failed" and result["record"] is None


# ---------------------------------------------------------------------------
# 5) 已存在学习记录（record_id）处理请求
# ---------------------------------------------------------------------------


@requires_schema
def test_submit_by_existing_record_id(tmp_path):
    store = StudyOCRStore(tmp_path)
    ref = _write_upload(store)
    image = store.paths.uploads_dir / ref

    photo = StudyStore(tmp_path / "study.db")
    photo.add_record(PhotoRecord("r1", str(image), "hash1", "2026-09-15"))

    adapter = _adapter(tmp_path, _record("r1", str(image)),
                       _audit("r1", str(image), mode="mock", ocr_status="mock",
                              model_status="mock"))
    adapter.photo_store = photo
    result = adapter.submit(record_id="r1")
    # OCR 业务 id 不透明且与学习记录 id 解耦；来源通过 source_record_id 保留。
    assert result["status"] == "completed"
    assert result["source_record_id"] == "r1"
    assert result["record_id"] != "r1"


def test_existing_record_outside_uploads_is_denied(tmp_path):
    store = StudyOCRStore(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_BYTES)
    photo = StudyStore(tmp_path / "study.db")
    photo.add_record(PhotoRecord("r2", str(outside), "hash2", "2026-09-15"))

    adapter = _adapter(tmp_path, _record("r2", str(outside)),
                       _audit("r2", str(outside), mode="mock", ocr_status="mock",
                              model_status="mock"))
    adapter.photo_store = photo
    with pytest.raises(OCRPathDenied):
        adapter.submit(record_id="r2")


def test_unknown_record_id_is_not_found(tmp_path):
    adapter = _adapter(tmp_path, None, None)
    with pytest.raises(OCRRecordNotFound):
        adapter.submit(record_id="does-not-exist")


# ---------------------------------------------------------------------------
# 6) 受控 API 契约与权限
# ---------------------------------------------------------------------------


def _client(adapter: OCRAdapter) -> TestClient:
    app = FastAPI()
    app.include_router(create_study_ocr_router(adapter))
    return TestClient(app)


def test_health_contract(tmp_path):
    adapter = _adapter(tmp_path, None, None)
    response = _client(adapter).get("/api/study/ocr/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded", "unavailable"}
    assert {"adapter", "learningocr", "honesty"} <= set(body)
    assert "uploads_dir" in body["adapter"] and "results_dir" in body["adapter"]


def test_submit_request_validation(tmp_path):
    client = _client(_adapter(tmp_path, None, None))
    assert client.post("/api/study/ocr/jobs", json={}).status_code == 422
    assert client.post("/api/study/ocr/jobs", json={
        "image_reference": "a.png", "record_id": "r1"}).status_code == 422
    assert client.post("/api/study/ocr/jobs", json={
        "image_reference": "a.png", "mode": "bogus"}).status_code == 422
    assert client.post("/api/study/ocr/jobs", json={
        "image_reference": "a.png", "route": "manual"}).status_code == 422


def test_submit_permission_and_not_found(tmp_path):
    client = _client(_adapter(tmp_path, None, None))
    assert client.post("/api/study/ocr/jobs", json={
        "image_reference": "../../etc/passwd"}).status_code == 403
    assert client.post("/api/study/ocr/jobs", json={
        "record_id": "missing"}).status_code == 404
    assert client.get("/api/study/ocr/jobs/missing").status_code == 404


@requires_schema
def test_submit_and_read_result_contract(tmp_path):
    store = StudyOCRStore(tmp_path)
    ref = _write_upload(store, "q9.png")
    image = str(store.paths.uploads_dir / ref)
    adapter = _adapter(tmp_path, _record("q9", image),
                       _audit("q9", image, mode="mock", ocr_status="mock",
                              model_status="mock"))
    client = _client(adapter)

    created = client.post("/api/study/ocr/jobs",
                          json={"image_reference": ref, "mode": "auto"})
    assert created.status_code == 200
    body = created.json()
    for key in ("record_id", "status", "mock", "real", "mode_effective",
                "ocr_status", "model_status", "validation", "record", "audit"):
        assert key in body
    assert body["mock"] is True and body["real"] is False

    fetched = client.get(f"/api/study/ocr/jobs/{body['record_id']}").json()
    assert fetched["record"]["id"] == body["record_id"]
    listed = client.get("/api/study/ocr/jobs").json()
    assert listed["jobs"] and "record" not in listed["jobs"][0]


def test_upload_endpoint_contract(tmp_path):
    client = _client(_adapter(tmp_path, None, None))
    ok = client.post("/api/study/ocr/uploads", params={"filename": "题1.png"},
                     content=PNG_BYTES)
    assert ok.status_code == 200
    ref = ok.json()["reference"]
    assert ".." not in ref and ref.endswith(".png")

    traversal = client.post("/api/study/ocr/uploads",
                            params={"filename": "../../evil.png"}, content=PNG_BYTES)
    assert traversal.status_code == 200
    assert ".." not in traversal.json()["reference"]

    bad = client.post("/api/study/ocr/uploads",
                      params={"filename": "notes.txt"}, content=PNG_BYTES)
    assert bad.status_code == 403


# ---------------------------------------------------------------------------
# 7) 前端契约（最小 OCR 面板）
# ---------------------------------------------------------------------------


def test_study_page_exposes_ocr_panel():
    study = _ROOT / "static" / "study"
    html = (study / "index.html").read_text(encoding="utf-8")
    js = (study / "app.js").read_text(encoding="utf-8")
    assert 'id="ocr"' in html and 'data-view="ocr"' in html
    assert "/api/study/ocr/jobs" in js
    assert "/api/study/ocr/health" in js
    assert "mock" in html + js and "real" in html + js


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
