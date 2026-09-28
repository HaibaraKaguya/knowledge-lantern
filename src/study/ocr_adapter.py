"""LearningOCR → 学习模块适配层（纯逻辑，不依赖 Web 框架）。

把外部 LearningOCR 项目中已验证的
``OCR → 视觉模型结构化 JSON`` 能力接进来，但**不复制模型权重、不复制 .venv**：
适配器只通过子进程调用源项目自己的 ``cli.py``（用它自己的解释器），
源项目保持只读。

保留源项目的关键不变量：

* **主记录 + 审计 sidecar 分离** —— 主记录面向界面，审计 sidecar 承载 OCR 证据、
  模型/mock 状态、清理日志与校验结果（见 :mod:`src.study.ocr_store`）。
* **真实/mock 明确标志** —— 结果显式给出 ``mock`` / ``real`` / ``mode_effective``，
  mock 永不冒充 real。
* **schema 校验** —— 用源项目 ``schema.py`` 独立复核主记录与审计，校验失败不伪造成功。
* **失败不伪造结果** —— 子进程报错、sidecar 缺失或 JSON 无法解析时，
  只记录 ``status="failed"``，不产出任何结构化结果。

路径安全：只有 :meth:`OCRAdapter.submit` 解析出的、位于受控上传目录内的图片才会
被读取；绝对路径、``..`` 穿越与符号链接逃逸一律拒绝（见
:meth:`StudyOCRPaths.resolve_upload`）。

身份与存储契约（本适配层收敛后）：

* **业务记录 id 不透明、不可覆盖** —— 由 :func:`src.study.ocr_store.new_record_id`
  生成（``ocr-<32hex>``），既不取自用户文件名 stem（旧缺陷会让同名不同内容互相
  覆盖），也不是 sha256。
* **sha256 只用于完整性校验与精确重复判定** —— 绝不作为用户可见业务 id。
* **物理上传路径按内容寻址** —— ``uploads/<sha256><后缀>``；用户文件名只作展示
  元数据（``image_filename``），不参与身份或路径。
* **精确重复策略** —— 同内容（sha256 相同）+ 同请求模式且校验通过时复用既有记录
  （幂等，响应 ``reused=true``）；内容/模式不同或既有记录失败则新建不可覆盖记录。
* **不暴露服务器绝对路径** —— 主记录/审计里的图片字段被改写为受控相对引用；
  API 只返回安全引用与记录 id。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .ocr_store import (
    OCRPathDenied,
    OCRRecordNotFound,
    StudyOCRStore,
    new_record_id,
)

logger = logging.getLogger("shuchiin.study.ocr")

MODES = ("auto", "mock", "real")
DEFAULT_ROOT = Path.home() / "LearningOCR" / "ocr-vlm-pipeline"
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
ALLOWED_UPLOAD_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


class OCRUnavailable(RuntimeError):
    """LearningOCR 源项目/解释器/入口缺失，无法运行。"""


class OCRRunFailed(RuntimeError):
    """子进程运行失败（非路径/记录类错误）。"""


# ---------------------------------------------------------------------------
# 源项目定位（环境变量驱动，绝不写死到代码里）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LearningOCRConfig:
    """源项目位置与运行参数；全部可由环境变量覆盖。"""

    root: Path
    python: Path
    timeout: int = 300

    @classmethod
    def from_env(cls, env: dict | None = None) -> "LearningOCRConfig":
        env = os.environ if env is None else env
        root = Path(env.get("LEARNINGOCR_ROOT") or DEFAULT_ROOT).expanduser()
        python_env = env.get("LEARNINGOCR_PYTHON")
        if python_env:
            python = Path(python_env).expanduser()
        else:
            venv_python = root / ".venv" / "bin" / "python"
            python = venv_python if venv_python.exists() else Path(sys.executable)
        try:
            timeout = int(env.get("LEARNINGOCR_TIMEOUT") or 300)
        except ValueError:
            timeout = 300
        return cls(root=root, python=python, timeout=timeout)

    @property
    def cli(self) -> Path:
        return self.root / "cli.py"


# ---------------------------------------------------------------------------
# 运行请求 / 结果
# ---------------------------------------------------------------------------


@dataclass
class RunRequest:
    image_path: Path
    mode: str
    problem_id: str
    output_path: Path
    audit_path: Path
    user_note: str | None = None
    subject: str | None = None


@dataclass
class RunResult:
    exit_code: int
    record: dict | None
    audit: dict | None
    stdout: str = ""
    stderr: str = ""


class PipelineRunner(Protocol):
    """可替换的运行器：生产用子进程，测试注入确定性 fake（不触模型）。"""

    def capabilities(self) -> dict: ...

    def run(self, request: RunRequest) -> RunResult: ...


# ---------------------------------------------------------------------------
# 子进程运行器：调用源项目自己的 cli.py（无 import 污染、无权重复制）
# ---------------------------------------------------------------------------


class SubprocessPipelineRunner:
    """用源项目的解释器执行 ``cli.py``，只读取它写出的 sidecar。"""

    def __init__(self, cfg: LearningOCRConfig):
        self.cfg = cfg
        self._caps: dict | None = None

    def available(self) -> bool:
        return self.cfg.root.is_dir() and self.cfg.cli.is_file()

    def capabilities(self) -> dict:
        """委托源项目 ``cli.py check-models --json``（不加载模型、不下载权重）。"""
        if self._caps is not None:
            return self._caps
        if not self.available():
            self._caps = self._unavailable("LearningOCR 项目或 cli.py 不存在")
            return self._caps
        try:
            proc = subprocess.run(
                [str(self.cfg.python), str(self.cfg.cli), "check-models", "--json"],
                cwd=str(self.cfg.root), capture_output=True, text=True,
                timeout=self.cfg.timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._caps = self._unavailable(f"无法运行 LearningOCR 能力探测: {exc}")
            return self._caps
        if proc.returncode != 0:
            self._caps = self._unavailable(
                f"check-models 退出码 {proc.returncode}: {_tail(proc.stderr)}")
            return self._caps
        try:
            report = json.loads(proc.stdout)
        except ValueError:
            self._caps = self._unavailable("check-models 输出不是合法 JSON")
            return self._caps
        self._caps = _map_capabilities(self.cfg, report)
        return self._caps

    def _unavailable(self, error: str) -> dict:
        return {
            "available": False,
            "root": str(self.cfg.root),
            "python": str(self.cfg.python),
            "cli": str(self.cfg.cli),
            "mock_available": self.available(),
            "real_available": False,
            "ocr": {"real_ready": False, "status": "unavailable"},
            "vlm": {"real_ready": False, "status": "unavailable"},
            "modes": ["mock", "auto"] if self.available() else [],
            "error": error,
        }

    def run(self, request: RunRequest) -> RunResult:
        if not self.available():
            raise OCRUnavailable("LearningOCR 项目或 cli.py 不存在")
        argv = [
            str(self.cfg.python), str(self.cfg.cli), "run", str(request.image_path),
            "-o", str(request.output_path), "--mode", request.mode,
            "--problem-id", request.problem_id,
        ]
        if request.subject:
            argv += ["--subject", request.subject]
        if request.user_note:
            argv += ["--user-note", request.user_note]
        try:
            proc = subprocess.run(
                argv, cwd=str(self.cfg.root), capture_output=True, text=True,
                timeout=self.cfg.timeout,
            )
        except subprocess.TimeoutExpired:
            return RunResult(exit_code=-1, record=None, audit=None,
                             stderr=f"LearningOCR 运行超时（>{self.cfg.timeout}s）")
        except OSError as exc:
            return RunResult(exit_code=-1, record=None, audit=None,
                             stderr=f"无法启动 LearningOCR: {exc}")
        return RunResult(
            exit_code=proc.returncode,
            record=_read_json(request.output_path),
            audit=_read_json(request.audit_path),
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )


def _map_capabilities(cfg: LearningOCRConfig, report: dict) -> dict:
    comps = report.get("components", {})
    det = comps.get("paddleocr_det", {})
    rec = comps.get("paddleocr_rec", {})
    qwen = comps.get("qwen2_5_vl_3b", {})
    ocr_ready = bool(det.get("complete")) and bool(rec.get("complete"))
    vlm_ready = bool(qwen.get("complete"))
    real_ready = ocr_ready and vlm_ready
    modes = ["mock", "auto"] + (["real"] if real_ready else [])
    return {
        "available": True,
        "root": str(cfg.root),
        "python": str(cfg.python),
        "cli": str(cfg.cli),
        "mock_available": True,
        "real_available": real_ready,
        "ocr": {
            "real_ready": ocr_ready,
            "status": (report.get("status") or {}).get("paddleocr"),
            "components": {"det": det, "rec": rec},
        },
        "vlm": {
            "real_ready": vlm_ready,
            "status": (report.get("status") or {}).get("qwen2_5_vl_3b"),
            "components": {"qwen2_5_vl_3b": qwen},
        },
        "models_root": report.get("models_root"),
        "modes": modes,
        "error": None,
        "note": (
            "mock 模式无需权重；real 模式需要 PaddleOCR 引擎与 Qwen2.5-VL 权重齐备。"
            "本接口不下载权重、不复制模型。"
        ),
    }


# ---------------------------------------------------------------------------
# schema 校验：只读加载源项目 schema.py（纯 stdlib），私有模块名避免污染
# ---------------------------------------------------------------------------

_SCHEMA_CACHE: dict[str, Any] = {}


def load_schema_module(root: str | Path):
    """以私有模块名加载 LearningOCR ``schema.py``，用于独立复核主记录/审计。

    直接编译源码执行，**不写 .pyc**，保证只读参考的源项目目录不被改动。
    """
    key = str(Path(root).resolve())
    if key in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[key]
    path = Path(root) / "schema.py"
    if not path.is_file():
        raise OCRUnavailable(f"LearningOCR schema.py 不存在: {path}")
    try:
        source = path.read_text(encoding="utf-8")
        code = compile(source, str(path), "exec")
    except (OSError, SyntaxError) as exc:
        raise OCRUnavailable(f"无法加载 LearningOCR schema.py: {exc}")
    module = types.ModuleType("_learningocr_schema")
    module.__file__ = str(path)
    exec(code, module.__dict__)
    _SCHEMA_CACHE[key] = module
    return module


# ---------------------------------------------------------------------------
# 适配器
# ---------------------------------------------------------------------------


class OCRAdapter:
    """受控入口：解析图片引用 → 运行源流水线 → schema 校验 → 分离落盘。"""

    def __init__(
        self,
        store: StudyOCRStore | None = None,
        *,
        cfg: LearningOCRConfig | None = None,
        runner: PipelineRunner | None = None,
        photo_store: Any | None = None,
    ):
        self.store = store or StudyOCRStore()
        self.cfg = cfg or LearningOCRConfig.from_env()
        self.runner = runner or SubprocessPipelineRunner(self.cfg)
        # 可选：学习记录库（StudyStore），用于按已存在记录 id 反查图片引用。
        self.photo_store = photo_store

    # -- 能力/健康 --------------------------------------------------------
    def capabilities(self) -> dict:
        source = self.runner.capabilities()
        if not source.get("available"):
            status = "unavailable"
        elif source.get("real_available"):
            status = "ok"
        else:
            status = "degraded"
        return {
            "status": status,
            # 只暴露受控存储的相对位置，绝不回传服务器绝对路径。
            "adapter": {
                "uploads_dir": self.store.paths.uploads_dir.name,
                "results_dir": self.store.paths.results_dir.name,
                "index_db": self.store.paths.index_db.name,
                "record_id": "不透明、不可覆盖（ocr-<hex>）；不取自文件名，也不是 sha256",
                "duplicate_policy": (
                    "同内容(sha256)+同请求模式且校验通过→复用既有记录(reused=true)；"
                    "内容/模式不同或既有记录失败→新建不可覆盖记录"
                ),
            },
            "learningocr": source,
            "honesty": (
                "mock 结果始终标记 mock；real 不可用时不会伪造 real，也不会下载模型。"
            ),
        }

    # -- 上传（内容寻址写入受控目录，返回相对引用） ------------------------
    def save_upload(self, filename: str, data: bytes) -> dict:
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise OCRPathDenied("上传内容为空")
        if len(data) > MAX_UPLOAD_BYTES:
            raise OCRPathDenied(f"图片超过上限 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
        safe = _safe_filename(filename)
        if safe is None:
            raise OCRPathDenied("非法文件名")
        suffix = Path(safe).suffix.lower()
        if suffix not in ALLOWED_UPLOAD_SUFFIXES:
            raise OCRPathDenied(f"不支持的图片类型: {suffix or '(无后缀)'}")
        sha = hashlib.sha256(bytes(data)).hexdigest()
        self.store.paths.ensure()
        # 物理路径按内容寻址（sha256 + 后缀）：用户文件名绝不参与身份或路径，
        # 只作为展示元数据保存；同内容重复上传只落盘一份。
        target = self.store.paths.uploads_dir / f"{sha}{suffix}"
        if not target.is_file():
            with open(target, "wb") as fh:
                fh.write(bytes(data))
        reference = self.store.paths.relative_reference(target)
        self.store.record_upload(sha, safe, len(data), reference)
        return {"reference": reference, "sha256": sha, "bytes": len(data),
                "filename": safe, "content_addressed": True}

    # -- 提交处理请求 -----------------------------------------------------
    def submit(
        self,
        *,
        image_reference: str | None = None,
        record_id: str | None = None,
        mode: str = "auto",
        user_note: str | None = None,
        subject: str | None = None,
    ) -> dict:
        if mode not in MODES:
            raise ValueError(f"mode 必须是 {list(MODES)} 之一")
        if bool(image_reference) == bool(record_id):
            raise ValueError("必须且只能提供 image_reference 或 record_id 之一")

        reference, source_record_id = self._resolve_reference(image_reference, record_id)
        image_path = self.store.paths.resolve_upload(reference)
        sha = _sha256_file(image_path)

        # 精确重复判定（sha256 只用于此 + 完整性校验）：命中则幂等复用，不新建、不覆盖。
        duplicate = self._find_reusable(sha, mode)
        if duplicate is not None:
            logger.info("study ocr reuse %s (sha=%s mode=%s)",
                        duplicate["record_id"], sha[:12], mode)
            return self._reuse_response(duplicate)

        job_id = new_record_id()
        display_name = self._display_filename(sha, reference)
        primary_path = self.store.paths.primary_path(job_id)
        audit_path = self.store.paths.audit_path(job_id)
        request = RunRequest(
            image_path=image_path, mode=mode, problem_id=job_id,
            output_path=primary_path, audit_path=audit_path,
            user_note=user_note, subject=subject,
        )

        try:
            result = self.runner.run(request)
        except Exception as exc:  # noqa: BLE001 - 失败必须落库，不能伪造结果
            return self._record_failure(
                job_id, reference, sha, mode, f"流水线运行异常: {exc}",
                display_name=display_name, source_record_id=source_record_id)

        if result.exit_code not in (0, 2) or result.record is None or result.audit is None:
            reason = _tail(result.stderr) or f"LearningOCR 退出码 {result.exit_code}"
            return self._record_failure(job_id, reference, sha, mode, reason,
                                        display_name=display_name,
                                        source_record_id=source_record_id)

        # 身份收敛 + 绝对路径抹除（在落盘/校验前完成，sidecar 与 API 都不含服务器路径）。
        _bind_identity(result.record, result.audit, job_id)
        _redact_image_paths(result.record, result.audit, reference)

        try:
            schema_mod = load_schema_module(self.cfg.root)
        except OCRUnavailable as exc:
            return self._record_failure(job_id, reference, sha, mode, str(exc),
                                        display_name=display_name,
                                        source_record_id=source_record_id)

        rec_valid, rec_errors, rec_warnings = schema_mod.validate_record(result.record)
        aud_valid, aud_errors, aud_warnings = schema_mod.validate_audit(result.audit)
        reported = result.audit.get("validation") or {}
        valid = bool(rec_valid and aud_valid and reported.get("valid"))
        errors = _dedupe(list(rec_errors) + list(aud_errors) + list(reported.get("errors") or []))
        warnings = _dedupe(list(rec_warnings) + list(aud_warnings)
                           + list(reported.get("warnings") or []))

        # 主记录 + 审计 sidecar 分离落盘（即使校验失败也保留，绝不静默丢弃）。
        self.store.write_sidecars(job_id, result.record, result.audit)

        audit = result.audit
        ocr_status = str((audit.get("ocr") or {}).get("status") or "unknown")
        model_status = str((audit.get("model") or {}).get("status") or "unknown")
        mode_effective = str((audit.get("model") or {}).get("mode") or "unknown")
        mock = _is_mock(mode_effective, ocr_status, model_status)
        status = "completed" if valid else "invalid"
        row = self._index_row(
            job_id, reference, sha, status=status, mode_requested=mode,
            mode_effective=mode_effective, ocr_status=ocr_status,
            model_status=model_status, mock=mock, valid=valid,
            error=None if valid else _tail("; ".join(errors)),
            primary_path=primary_path, audit_path=audit_path,
            image_filename=display_name, source_record_id=source_record_id,
        )
        self.store.upsert_index(row)
        logger.info("study ocr job %s: status=%s mode=%s mock=%s",
                    job_id, status, mode_effective, mock)
        return self._response(row, result.record, audit,
                              validation={"valid": valid, "errors": errors,
                                          "warnings": warnings})

    # -- 读取结果 ---------------------------------------------------------
    def get_result(self, record_id: str, *, include_audit: bool = True) -> dict:
        row = self.store.get_index(record_id)
        record = None
        audit = None
        try:
            record = self.store.read_primary(record_id)
        except OCRRecordNotFound:
            record = None
        if include_audit:
            try:
                audit = self.store.read_audit(record_id)
            except OCRRecordNotFound:
                audit = None
        validation = {
            "valid": bool(row.get("valid")),
            "errors": [],
            "warnings": [],
        }
        if audit is not None:
            reported = audit.get("validation") or {}
            validation = {
                "valid": bool(reported.get("valid")) and bool(row.get("valid")),
                "errors": list(reported.get("errors") or []),
                "warnings": list(reported.get("warnings") or []),
            }
        return self._response(row, record, audit, validation=validation)

    def list_jobs(self, limit: int = 50) -> list[dict]:
        return [self._job_summary(row) for row in self.store.list_index(limit)]

    # -- 内部 -------------------------------------------------------------
    def _resolve_reference(
        self, image_reference: str | None, record_id: str | None
    ) -> tuple[str, str | None]:
        """返回 (受控相对引用, 来源学习记录 id 或 None)。不读取任何目录外的文件。"""
        if image_reference:
            return image_reference, None

        assert record_id is not None
        photo_ref = self._photo_reference(record_id)
        if photo_ref is not None:
            reference = self.store.paths.relative_reference(photo_ref)
            return reference, record_id
        if self.store.has_index(record_id):
            row = self.store.get_index(record_id)
            return row["image_reference"], row.get("source_record_id")
        raise OCRRecordNotFound(record_id)

    def _display_filename(self, sha: str, reference: str) -> str:
        """展示用文件名：优先取上传时登记的原始名，否则退回引用 basename。"""
        upload = self.store.get_upload(sha)
        if upload and upload.get("filename"):
            return str(upload["filename"])
        return Path(reference).name

    def _find_reusable(self, sha: str, mode: str) -> dict | None:
        """同内容 + 同模式且 sidecar 齐备的既有成功记录（否则视为不可复用）。"""
        row = self.store.find_duplicate(sha, mode)
        if row is None:
            return None
        try:
            self.store.read_primary(row["record_id"])
            self.store.read_audit(row["record_id"])
        except OCRRecordNotFound:
            return None
        return row

    def _reuse_response(self, row: dict) -> dict:
        record = self.store.read_primary(row["record_id"])
        audit = self.store.read_audit(row["record_id"])
        reported = audit.get("validation") or {}
        validation = {
            "valid": bool(reported.get("valid")) and bool(row.get("valid")),
            "errors": list(reported.get("errors") or []),
            "warnings": list(reported.get("warnings") or []),
        }
        return self._response(row, record, audit, validation=validation, reused=True)

    def _photo_reference(self, record_id: str) -> Path | None:
        if self.photo_store is None:
            return None
        try:
            row = self.photo_store.conn.execute(
                "SELECT image_path FROM study_records WHERE record_id=?", (record_id,)
            ).fetchone()
        except Exception:  # noqa: BLE001 - 学习库不可用时按“无此记录”处理
            return None
        if row is None:
            return None
        return Path(row["image_path"])

    def _record_failure(
        self, job_id: str, reference: str, sha: str, mode: str, error: str, *,
        display_name: str | None = None, source_record_id: str | None = None,
    ) -> dict:
        logger.warning("study ocr job %s failed: %s", job_id, _tail(error, 200))
        row = self._index_row(
            job_id, reference, sha, status="failed", mode_requested=mode,
            mode_effective="error", ocr_status="error", model_status="error",
            mock=False, valid=False, error=_tail(error, 500),
            primary_path=self.store.paths.primary_path(job_id),
            audit_path=self.store.paths.audit_path(job_id),
            image_filename=display_name or Path(reference).name,
            source_record_id=source_record_id,
        )
        self.store.upsert_index(row)
        return self._response(row, None, None,
                              validation={"valid": False, "errors": [row["error"]],
                                          "warnings": []})

    def _index_row(self, job_id: str, reference: str, sha: str, **kw: Any) -> dict:
        return {
            "record_id": job_id,
            "image_reference": reference,
            "image_sha256": sha,
            "status": kw["status"],
            "mode_requested": kw["mode_requested"],
            "mode_effective": kw["mode_effective"],
            "ocr_status": kw["ocr_status"],
            "model_status": kw["model_status"],
            "mock": int(bool(kw["mock"])),
            "valid": int(bool(kw["valid"])),
            "error": kw["error"],
            "created_at": _now_iso(),
            "primary_path": str(kw["primary_path"]),
            "audit_path": str(kw["audit_path"]),
            "image_filename": kw.get("image_filename"),
            "source_record_id": kw.get("source_record_id"),
        }

    def _response(
        self, row: dict, record: dict | None, audit: dict | None, *,
        validation: dict, reused: bool = False,
    ) -> dict:
        mock = bool(row.get("mock"))
        real = (not mock) and row.get("model_status") == "real" and row.get("ocr_status") == "ok"
        return {
            "record_id": row["record_id"],
            "status": row["status"],
            "mock": mock,
            "real": real,
            "reused": reused,
            "mode_requested": row["mode_requested"],
            "mode_effective": row["mode_effective"],
            "ocr_status": row["ocr_status"],
            "model_status": row["model_status"],
            "image_reference": row["image_reference"],
            "image_filename": row.get("image_filename"),
            "image_sha256": row["image_sha256"],
            "source_record_id": row.get("source_record_id"),
            "created_at": row["created_at"],
            "validation": validation,
            "record": record,
            "audit": audit,
            "error": row.get("error"),
        }

    def _job_summary(self, row: dict) -> dict:
        return {
            "record_id": row["record_id"],
            "status": row["status"],
            "mock": bool(row["mock"]),
            "mode_requested": row["mode_requested"],
            "mode_effective": row["mode_effective"],
            "ocr_status": row["ocr_status"],
            "model_status": row["model_status"],
            "image_reference": row["image_reference"],
            "image_filename": row.get("image_filename"),
            "image_sha256": row["image_sha256"],
            "created_at": row["created_at"],
        }


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _is_mock(mode_effective: str, ocr_status: str, model_status: str) -> bool:
    return mode_effective == "mock" or ocr_status == "mock" or model_status == "mock"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _bind_identity(record: dict | None, audit: dict | None, record_id: str) -> None:
    """强制主记录 ``id`` / 审计 ``record_id`` 与业务记录 id 一致（身份收敛）。"""
    if isinstance(record, dict):
        record["id"] = record_id
    if isinstance(audit, dict):
        audit["record_id"] = record_id


def _redact_image_paths(record: dict | None, audit: dict | None, reference: str) -> None:
    """把主记录/审计里由源流水线写回的服务器绝对路径改写为受控相对引用。"""
    if isinstance(record, dict) and isinstance(record.get("image"), str):
        record["image"] = reference
    if isinstance(audit, dict):
        source = audit.get("source")
        if isinstance(source, dict) and isinstance(source.get("image"), str):
            source["image"] = reference


def _safe_filename(filename: str) -> str | None:
    if not isinstance(filename, str) or not filename.strip():
        return None
    name = filename.replace("\\", "/").split("/")[-1].strip()
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)
    cleaned = cleaned.lstrip("._")
    return cleaned[:120] or None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _read_json(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _tail(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[-limit:]
