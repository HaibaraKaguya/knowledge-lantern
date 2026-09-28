"""学习 OCR 身份 / 存储契约测试 —— 覆盖新 ID 机制、内容寻址与精确重复策略。

覆盖（全部使用确定性 fake 流水线，无真实模型、无真实图片解码）：

* **同名不同内容不覆盖**：`q1.png` 换成不同字节后再次提交，得到不同的不透明记录 id，
  两条结果都独立存在；
* **异名相同内容遵循重复策略**：同内容 + 同模式 → 幂等复用（`reused=true`），不新建；
* **记录 id 不透明**：不取自文件名 stem，也不是 sha256；
* **物理上传路径按内容寻址**：用户文件名只作展示元数据，不参与路径/身份；
* **路径安全**：上传/提交拒绝绝对路径、`..` 穿越；
* **API 无服务器绝对路径泄露**：主记录/审计/列表/健康响应与磁盘 sidecar 均不含
  上传目录绝对路径；
* **主/审计 sidecar 分离**：两份文件、索引只存元数据；
* **旧 `q1.json` 兼容**：旧文件名-stem 记录仍可按原 id 读取，旧库缺列自动迁移。

fake 运行器与 schema 复核复用 `tests/test_study_ocr_adapter.py` 中的同一套夹具
（schema 缺失时相关用例跳过）。
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
for _p in (_ROOT, _ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from src.study.ocr_adapter import OCRAdapter  # noqa: E402
from src.study.ocr_api import create_study_ocr_router  # noqa: E402
from src.study.ocr_store import (  # noqa: E402
    OCRPathDenied,
    StudyOCRStore,
    new_record_id,
    valid_record_id,
)

# 复用现有测试的确定性 fake（无模型、无图片解码）与合成 PNG。
from test_study_ocr_adapter import (  # noqa: E402
    PNG_BYTES,
    _adapter,
    _audit,
    _record,
    _write_upload,
    requires_schema,
)

# 与 PNG_BYTES 同名但内容不同（追加一个字节 → 不同 sha256）。
PNG_BYTES_B = PNG_BYTES + b"\x00"


def _client(adapter: OCRAdapter) -> TestClient:
    app = FastAPI()
    app.include_router(create_study_ocr_router(adapter))
    return TestClient(app)


# ---------------------------------------------------------------------------
# 1) 记录 id 不透明、不可覆盖
# ---------------------------------------------------------------------------


def test_new_record_id_is_unique_and_valid():
    ids = {new_record_id() for _ in range(64)}
    assert len(ids) == 64
    assert all(valid_record_id(i) for i in ids)
    assert all(i.startswith("ocr-") for i in ids)


@requires_schema
def test_same_filename_different_content_does_not_overwrite(tmp_path):
    store = StudyOCRStore(tmp_path)
    store.paths.ensure()
    upload = store.paths.uploads_dir / "q1.png"

    # 第一次：q1.png = A
    upload.write_bytes(PNG_BYTES)
    image = str(upload)
    first = _adapter(tmp_path, _record("q1", image),
                     _audit("q1", image, mode="mock", ocr_status="mock",
                            model_status="mock")).submit(image_reference="q1.png")
    assert first["status"] == "completed"

    # 第二次：同名 q1.png，但内容不同（B）—— 旧缺陷会覆盖第一条结果。
    upload.write_bytes(PNG_BYTES_B)
    second = _adapter(tmp_path, _record("q1", image),
                      _audit("q1", image, mode="mock", ocr_status="mock",
                             model_status="mock")).submit(image_reference="q1.png")

    assert first["record_id"] != second["record_id"]
    assert first["image_sha256"] != second["image_sha256"]
    assert first["reused"] is False and second["reused"] is False
    # 两条记录都独立落盘、互不覆盖。
    assert store.read_primary(first["record_id"])["id"] == first["record_id"]
    assert store.read_primary(second["record_id"])["id"] == second["record_id"]
    assert store.get_index(first["record_id"])["image_sha256"] == first["image_sha256"]
    assert store.get_index(second["record_id"])["image_sha256"] == second["image_sha256"]
    assert store.paths.primary_path(first["record_id"]).is_file()
    assert store.paths.primary_path(second["record_id"]).is_file()


@requires_schema
def test_record_id_is_neither_filename_nor_sha256(tmp_path):
    store = StudyOCRStore(tmp_path)
    adapter = _adapter(tmp_path, None, None)
    up = adapter.save_upload("q1.png", PNG_BYTES)
    assert up["filename"] == "q1.png"                     # 文件名仅展示
    assert up["reference"] == f"{up['sha256']}.png"       # 物理路径按内容寻址
    assert "q1" not in up["reference"]

    image = str(store.paths.resolve_upload(up["reference"]))
    result = _adapter(tmp_path, _record("q1", image),
                      _audit("q1", image, mode="mock", ocr_status="mock",
                             model_status="mock")).submit(image_reference=up["reference"])
    rid = result["record_id"]
    assert valid_record_id(rid) and rid.startswith("ocr-")
    assert rid != up["sha256"] and up["sha256"] not in rid   # 不是 sha256
    assert "q1" not in rid                                   # 不取自文件名


# ---------------------------------------------------------------------------
# 2) 精确重复策略（异名相同内容）
# ---------------------------------------------------------------------------


@requires_schema
def test_same_content_different_name_reuses_existing_record(tmp_path):
    store = StudyOCRStore(tmp_path)
    store.paths.ensure()
    (store.paths.uploads_dir / "a.png").write_bytes(PNG_BYTES)
    (store.paths.uploads_dir / "b.png").write_bytes(PNG_BYTES)  # 异名同内容

    image = str(store.paths.uploads_dir / "a.png")
    adapter = _adapter(tmp_path, _record("x", image),
                       _audit("x", image, mode="mock", ocr_status="mock",
                              model_status="mock"))
    first = adapter.submit(image_reference="a.png")
    second = adapter.submit(image_reference="b.png")

    assert first["reused"] is False
    assert second["reused"] is True
    assert second["record_id"] == first["record_id"]   # 幂等复用，不新建、不覆盖
    assert second["image_sha256"] == first["image_sha256"]
    assert len(store.list_index()) == 1
    # 复用时返回既有 sidecar 内容。
    assert second["record"]["id"] == first["record_id"]
    assert second["audit"]["kind"] == "audit"


@requires_schema
def test_duplicate_policy_does_not_reuse_failed_records(tmp_path):
    store = StudyOCRStore(tmp_path)
    ref = _write_upload(store, "q1.png")

    failed = _adapter(tmp_path, None, None, exit_code=1).submit(image_reference=ref)
    assert failed["status"] == "failed"

    # 同内容重试：失败记录不复用（不阻塞重试），新建不可覆盖记录。
    image = str(store.paths.uploads_dir / ref)
    retried = _adapter(tmp_path, _record("q1", image),
                       _audit("q1", image, mode="mock", ocr_status="mock",
                              model_status="mock")).submit(image_reference=ref)
    assert retried["status"] == "completed"
    assert retried["reused"] is False
    assert retried["record_id"] != failed["record_id"]


# ---------------------------------------------------------------------------
# 3) 物理上传路径按内容寻址（文件名只作展示元数据）
# ---------------------------------------------------------------------------


def test_upload_path_is_content_addressed_not_filename(tmp_path):
    adapter = _adapter(tmp_path, None, None)
    a = adapter.save_upload("题 1.png", PNG_BYTES)
    b = adapter.save_upload("../../evil.png", PNG_BYTES_B)

    # 物理引用 = <sha256><后缀>，不含用户文件名；同名/穿越名只清洗为展示元数据。
    assert a["reference"] == f"{a['sha256']}.png"
    assert b["reference"] == f"{b['sha256']}.png"
    for up in (a, b):
        assert ".." not in up["reference"] and "evil" not in up["reference"]
        assert not Path(up["reference"]).is_absolute()
    assert a["filename"] == "题_1.png"
    assert b["filename"] == "evil.png"
    assert adapter.store.get_upload(a["sha256"])["filename"] == "题_1.png"
    assert adapter.store.get_upload(b["sha256"])["filename"] == "evil.png"

    # 同内容重复上传复用同一物理路径，只落盘一份。
    dup = adapter.save_upload("again.png", PNG_BYTES)
    assert dup["reference"] == a["reference"]
    files = sorted(p.name for p in adapter.store.paths.uploads_dir.iterdir() if p.is_file())
    assert files == sorted([a["reference"], b["reference"]])


def test_upload_and_submit_reject_path_escapes(tmp_path):
    adapter = _adapter(tmp_path, None, None)
    with pytest.raises(OCRPathDenied):
        adapter.save_upload("notes.txt", PNG_BYTES)
    with pytest.raises(OCRPathDenied):
        adapter.save_upload("q1.png", b"")

    ref = adapter.save_upload("q1.png", PNG_BYTES)["reference"]
    assert not Path(ref).is_absolute()
    for bad in ("../q1.png", "/etc/passwd", "../../etc/passwd", "sub/../../q1.png"):
        with pytest.raises(OCRPathDenied):
            adapter.submit(image_reference=bad)


# ---------------------------------------------------------------------------
# 4) API 不暴露服务器绝对路径
# ---------------------------------------------------------------------------


@requires_schema
def test_api_never_exposes_absolute_image_paths(tmp_path):
    store = StudyOCRStore(tmp_path)
    ref = _write_upload(store, "q1.png")
    image = str(store.paths.uploads_dir / ref)
    adapter = _adapter(tmp_path, _record("q1", image),
                       _audit("q1", image, mode="mock", ocr_status="mock",
                              model_status="mock"))
    client = _client(adapter)
    abs_dir = str(store.paths.uploads_dir.resolve())

    body = client.post("/api/study/ocr/jobs", json={"image_reference": ref}).json()
    assert abs_dir not in json.dumps(body, ensure_ascii=False)
    assert body["record"]["image"] == ref                  # 安全引用，非绝对路径
    assert body["audit"]["source"]["image"] == ref

    fetched = client.get(f"/api/study/ocr/jobs/{body['record_id']}").json()
    assert abs_dir not in json.dumps(fetched, ensure_ascii=False)

    listed = client.get("/api/study/ocr/jobs").json()
    assert abs_dir not in json.dumps(listed, ensure_ascii=False)

    health = client.get("/api/study/ocr/health").json()
    assert abs_dir not in json.dumps(health, ensure_ascii=False)
    assert health["adapter"]["uploads_dir"] == "uploads"   # 只回传相对目录名

    # 磁盘 sidecar 也不含上传目录绝对路径。
    primary = store.paths.primary_path(body["record_id"]).read_text(encoding="utf-8")
    audit = store.paths.audit_path(body["record_id"]).read_text(encoding="utf-8")
    assert abs_dir not in primary and abs_dir not in audit


@requires_schema
def test_sidecars_separated_and_paths_redacted(tmp_path):
    store = StudyOCRStore(tmp_path)
    ref = _write_upload(store, "q1.png")
    image = str(store.paths.uploads_dir / ref)
    result = _adapter(tmp_path, _record("q1", image),
                      _audit("q1", image, mode="mock", ocr_status="mock",
                             model_status="mock")).submit(image_reference=ref)
    rid = result["record_id"]

    primary_path = store.paths.primary_path(rid)
    audit_path = store.paths.audit_path(rid)
    assert primary_path.is_file() and audit_path.is_file()
    assert primary_path != audit_path

    primary = json.loads(primary_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert "ocr" not in primary and "validation" not in primary
    assert audit["kind"] == "audit" and audit["record_id"] == rid
    assert primary["image"] == ref and audit["source"]["image"] == ref
    assert store.get_index(rid)["image_filename"] == "q1.png"   # 文件名仅展示元数据


# ---------------------------------------------------------------------------
# 5) 旧文件名-stem 记录兼容 + 轻量迁移
# ---------------------------------------------------------------------------


def test_legacy_filename_stem_record_is_still_readable(tmp_path):
    store = StudyOCRStore(tmp_path)
    store.paths.ensure()
    legacy = {
        "id": "q1", "subject": "limits",
        "question": {"text": "t", "formulas": [], "requirements": []},
        "knowledge_points": [], "user_note": {"text": None, "knowledge_gap": None},
        "retrieval_text": "r", "image": "q1.png",
    }
    audit = {"kind": "audit", "record_id": "q1",
             "validation": {"valid": True, "errors": [], "warnings": []}}
    store.write_sidecars("q1", legacy, audit)
    store.upsert_index({
        "record_id": "q1", "image_reference": "q1.png", "image_sha256": "a" * 64,
        "status": "completed", "mode_requested": "mock", "mode_effective": "mock",
        "ocr_status": "mock", "model_status": "mock", "mock": 1, "valid": 1,
        "error": None, "created_at": "2026-09-15T00:00:00+00:00",
        "primary_path": str(store.paths.primary_path("q1")),
        "audit_path": str(store.paths.audit_path("q1")),
        "image_filename": "q1.png", "source_record_id": None,
    })

    result = _adapter(tmp_path, None, None).get_result("q1")
    assert result["record_id"] == "q1"
    assert result["record"]["id"] == "q1"
    assert result["audit"]["kind"] == "audit"
    assert store.paths.primary_path("q1").name == "q1.json"


def test_index_migration_adds_columns_to_old_db(tmp_path):
    # 模拟旧版 index.db：表存在但缺 image_filename / source_record_id 两列。
    conn = sqlite3.connect(tmp_path / "index.db")
    conn.execute(
        "CREATE TABLE study_ocr_jobs ("
        "record_id TEXT PRIMARY KEY, image_reference TEXT NOT NULL,"
        "image_sha256 TEXT NOT NULL, status TEXT NOT NULL, mode_requested TEXT NOT NULL,"
        "mode_effective TEXT NOT NULL, ocr_status TEXT NOT NULL, model_status TEXT NOT NULL,"
        "mock INTEGER NOT NULL, valid INTEGER NOT NULL, error TEXT, created_at TEXT NOT NULL,"
        "primary_path TEXT NOT NULL, audit_path TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO study_ocr_jobs VALUES "
        "('q1','q1.png',?, 'completed','mock','mock','mock','mock',1,1,NULL,"
        "'2026-09-15T00:00:00+00:00','p','a')",
        ("a" * 64,),
    )
    conn.commit()
    conn.close()

    store = StudyOCRStore(tmp_path)          # 首次访问触发 _migrate
    row = store.get_index("q1")
    assert store.has_index("q1")             # 旧行保留、不删除
    assert row["image_filename"] is None and row["source_record_id"] is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
