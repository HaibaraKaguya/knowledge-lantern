"""受控 OCR 存储：主记录 + 审计 sidecar 分离，索引仅元数据。

设计约束（与 LearningOCR 的 primary record / audit sidecar 分离保持一致）：

* 上传图片只落在受控 ``uploads/`` 目录；读取引用必须落在该目录内 ——
  拒绝绝对路径、``..`` 穿越与符号链接逃逸，禁止任意文件读取。
* 物理上传路径按**内容寻址**（``<sha256><后缀>``）：用户文件名只作展示元数据
  （``study_ocr_uploads`` 表 + 任务行 ``image_filename``），绝不参与身份或路径。
* OCR 业务记录 id 是**不透明、不可覆盖**的（:func:`new_record_id`）：既不取自
  文件名 stem，也不是 sha256 本身。sha256 只用于完整性校验与精确重复判定。
* 每次处理产出两份 JSON sidecar：``<record_id>.json``（主记录，面向界面）与
  ``<record_id>.audit.json``（审计 sidecar，含 OCR 证据与模型/mock 状态）。
* SQLite 索引只存元数据（记录 id、相对图片引用、展示文件名、sha256、状态、模式、
  校验结果、sidecar 路径）；**绝不写入图片二进制，也不写入 OCR 原文**。

兼容：旧版按文件名 stem 命名的主记录（如 ``results/q1.json`` + 索引行 ``q1``）仍是
合法记录 id，可继续用原 id 读取；本模块不做破坏性迁移，只在缺列时做轻量 ``ALTER``。
"""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
import uuid
from pathlib import Path

from src.utils.files import DATA_DIR

# record_id 允许的字符集：防止 id 本身成为穿越向量。
_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# 索引表结构。新增列通过 :func:`_migrate` 以 ALTER 方式补齐（旧 index.db 可平滑升级）。
_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS study_ocr_jobs (
    record_id       TEXT PRIMARY KEY,
    image_reference TEXT NOT NULL,
    image_sha256    TEXT NOT NULL,
    status          TEXT NOT NULL,
    mode_requested  TEXT NOT NULL,
    mode_effective  TEXT NOT NULL,
    ocr_status      TEXT NOT NULL,
    model_status    TEXT NOT NULL,
    mock            INTEGER NOT NULL,
    valid           INTEGER NOT NULL,
    error           TEXT,
    created_at      TEXT NOT NULL,
    primary_path    TEXT NOT NULL,
    audit_path      TEXT NOT NULL,
    image_filename  TEXT,
    source_record_id TEXT
);
CREATE INDEX IF NOT EXISTS study_ocr_jobs_status ON study_ocr_jobs(status);
CREATE INDEX IF NOT EXISTS study_ocr_jobs_sha ON study_ocr_jobs(image_sha256);
CREATE TABLE IF NOT EXISTS study_ocr_uploads (
    sha256     TEXT PRIMARY KEY,
    filename   TEXT,
    bytes      INTEGER NOT NULL,
    reference  TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# 旧库缺列时按 (列名, 声明) 轻量补齐；仅内部常量，无注入风险。
_ADDED_COLUMNS = (
    ("image_filename", "TEXT"),
    ("source_record_id", "TEXT"),
)


class OCRPathDenied(ValueError):
    """引用路径不在受控上传目录内（或形态非法）时抛出。"""


class OCRRecordNotFound(KeyError):
    """请求的记录/处理结果不存在时抛出。"""


def valid_record_id(record_id: str) -> bool:
    """record_id 必须是安全的短标识符，不能是路径。"""
    return isinstance(record_id, str) and bool(_RECORD_ID_RE.match(record_id))


def new_record_id() -> str:
    """生成不透明、不可覆盖的业务记录 id。

    与文件名、内容哈希解耦：既不取自用户文件名 stem（旧缺陷会因此互相覆盖），
    也不是 sha256（哈希只用于完整性校验与精确重复判定）。内容相同→相同 id 由
    :meth:`StudyOCRStore.find_duplicate` 的精确重复策略保证，而非由 id 生成方式保证。
    """
    return f"ocr-{uuid.uuid4().hex}"


class StudyOCRPaths:
    """受控目录规则：``uploads/`` 只读放图，``results/`` 放 sidecar，``index.db`` 放元数据。"""

    def __init__(self, base_dir: str | Path | None = None):
        self.base = Path(base_dir) if base_dir else Path(DATA_DIR) / "study_ocr"

    @property
    def uploads_dir(self) -> Path:
        return self.base / "uploads"

    @property
    def results_dir(self) -> Path:
        return self.base / "results"

    @property
    def index_db(self) -> Path:
        return self.base / "index.db"

    def ensure(self) -> None:
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

    # -- 上传引用 ---------------------------------------------------------
    def resolve_upload(self, reference: str) -> Path:
        """把客户端引用解析为受控 uploads/ 内的真实文件路径。

        拒绝：空引用、绝对路径、``..`` 穿越、符号链接逃逸、非普通文件。
        """
        if not isinstance(reference, str) or not reference.strip():
            raise OCRPathDenied("图片引用不能为空")
        if "\x00" in reference:
            raise OCRPathDenied("图片引用包含非法字符")
        raw = reference.strip().replace("\\", "/")
        candidate = Path(raw)
        if candidate.is_absolute() or (len(raw) > 1 and raw[1] == ":"):
            raise OCRPathDenied("图片引用必须是受控上传目录内的相对路径")

        base = self.uploads_dir.resolve()
        resolved = (base / candidate).resolve()
        if resolved == base or not resolved.is_relative_to(base):
            raise OCRPathDenied("图片引用超出允许的学习上传目录")
        if not resolved.is_file():
            raise OCRPathDenied("图片引用不存在或不是文件")
        return resolved

    def relative_reference(self, path: str | Path) -> str:
        """把受控目录内的真实路径还原为可持久化的相对引用。"""
        base = self.uploads_dir.resolve()
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(base):
            raise OCRPathDenied("图片不在受控上传目录内")
        return resolved.relative_to(base).as_posix()

    # -- sidecar 路径 -----------------------------------------------------
    def _require_id(self, record_id: str) -> str:
        if not valid_record_id(record_id):
            raise OCRPathDenied(f"非法记录 id: {record_id!r}")
        return record_id

    def primary_path(self, record_id: str) -> Path:
        return self.results_dir / f"{self._require_id(record_id)}.json"

    def audit_path(self, record_id: str) -> Path:
        return self.results_dir / f"{self._require_id(record_id)}.audit.json"


class StudyOCRStore:
    """sidecar 落盘 + 元数据索引。构造时不触盘，首次写入才建目录。"""

    def __init__(self, base_dir: str | Path | None = None):
        self.paths = StudyOCRPaths(base_dir)
        self._conn: sqlite3.Connection | None = None

    # -- 连接 -------------------------------------------------------------
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.paths.ensure()
            self._conn = sqlite3.connect(str(self.paths.index_db), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_INDEX_SCHEMA)
            _migrate(self._conn)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- 上传元数据（文件名仅作展示，不参与身份） --------------------------
    def record_upload(self, sha256: str, filename: str | None, nbytes: int,
                      reference: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO study_ocr_uploads VALUES "
            "(:sha256,:filename,:bytes,:reference,:created_at)",
            {"sha256": sha256, "filename": filename, "bytes": int(nbytes),
             "reference": reference, "created_at": _now_iso()},
        )
        self.conn.commit()

    def get_upload(self, sha256: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM study_ocr_uploads WHERE sha256=?", (sha256,)
        ).fetchone()
        return dict(row) if row else None

    # -- sidecar ----------------------------------------------------------
    def write_sidecars(self, record_id: str, record: dict, audit: dict) -> tuple[Path, Path]:
        self.paths.ensure()
        primary, audit_path = self.paths.primary_path(record_id), self.paths.audit_path(record_id)
        _dump(record, primary)
        _dump(audit, audit_path)
        return primary, audit_path

    def read_primary(self, record_id: str) -> dict:
        path = self.paths.primary_path(record_id)
        if not path.is_file():
            raise OCRRecordNotFound(record_id)
        return _load(path)

    def read_audit(self, record_id: str) -> dict:
        path = self.paths.audit_path(record_id)
        if not path.is_file():
            raise OCRRecordNotFound(record_id)
        return _load(path)

    # -- 索引 -------------------------------------------------------------
    def upsert_index(self, row: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO study_ocr_jobs VALUES "
            "(:record_id,:image_reference,:image_sha256,:status,:mode_requested,"
            ":mode_effective,:ocr_status,:model_status,:mock,:valid,:error,"
            ":created_at,:primary_path,:audit_path,:image_filename,:source_record_id)",
            row,
        )
        self.conn.commit()

    def get_index(self, record_id: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM study_ocr_jobs WHERE record_id=?", (record_id,)
        ).fetchone()
        if row is None:
            raise OCRRecordNotFound(record_id)
        return dict(row)

    def list_index(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM study_ocr_jobs ORDER BY created_at DESC, record_id LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(row) for row in rows]

    def has_index(self, record_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM study_ocr_jobs WHERE record_id=?", (record_id,)
        ).fetchone() is not None

    def find_duplicate(self, image_sha256: str, mode_requested: str) -> dict | None:
        """精确重复判定：返回同内容 + 同请求模式、且校验通过的最新既有记录。

        只复用**成功**（``valid=1``）的记录，失败记录不阻塞重试。sha256 在此仅用于
        精确重复判定（完整性校验），绝不作为业务 id。
        """
        row = self.conn.execute(
            "SELECT * FROM study_ocr_jobs WHERE image_sha256=? AND mode_requested=? "
            "AND valid=1 ORDER BY created_at DESC, record_id DESC LIMIT 1",
            (image_sha256, mode_requested),
        ).fetchone()
        return dict(row) if row else None


def _migrate(conn: sqlite3.Connection) -> None:
    """旧 index.db 缺列时轻量补列（不删数据、不改既有行）。"""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(study_ocr_jobs)")}
    for column, decl in _ADDED_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE study_ocr_jobs ADD COLUMN {column} {decl}")
    conn.commit()


def _dump(obj: dict, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _load(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
