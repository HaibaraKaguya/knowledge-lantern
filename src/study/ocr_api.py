"""学习模块 OCR 接入的 HTTP 边界（受控 API）。

端点（前缀 ``/api/study/ocr``）：

* ``GET  /health``              —— 适配器 + LearningOCR 能力状态（mock/real 可用性）。
* ``POST /jobs``                —— 提交处理请求：受控上传目录内的图片引用，
                                   或已存在学习记录 id（二选一，禁止任意文件读取）。
* ``GET  /jobs``                —— 列出已处理任务（仅元数据，无 OCR 原文）。
* ``GET  /jobs/{record_id}``    —— 读取结构化结果（主记录 + 审计 sidecar + 校验）。
* ``POST /uploads``             —— 把本地图片写入受控上传目录，返回可提交的相对引用。

身份与存储契约：

* ``record_id`` 是**不透明、不可覆盖**的业务 id（``ocr-<hex>``）：不取自文件名 stem，
  也不是 sha256。同内容重复上传不会覆盖既有结果。
* sha256 只用于**完整性校验**与**精确重复判定**，不作为用户可见业务 id。
* 物理上传路径按内容寻址（``<sha256><后缀>``）；用户文件名仅作展示元数据
  （``image_filename``），不参与身份或路径。
* 响应与主记录/审计 sidecar **不含服务器绝对路径**：图片字段被改写为受控相对引用，
  对外只给安全引用（``image_reference``）与记录 id。
* **精确重复策略**：同内容（sha256 相同）+ 同请求模式且校验通过时，``POST /jobs``
  幂等复用既有记录并返回 ``reused=true``；内容/模式不同或既有记录失败则新建记录。

错误语义：403 路径越界（穿越/绝对路径/符号链接逃逸）· 404 记录不存在 ·
400 输入非法 · 422 请求结构非法（``extra=forbid``）· 503 源项目不可用。
处理失败（模型/流水线报错）仍返回 200，但 ``status="failed"`` 且不产出结果 ——
失败绝不伪造成功。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ocr_adapter import MODES, OCRAdapter, OCRUnavailable
from .ocr_store import OCRPathDenied, OCRRecordNotFound


class StudyOCRSubmitRequest(BaseModel):
    """处理请求：``image_reference`` 与 ``record_id`` 必须二选一。"""

    model_config = ConfigDict(extra="forbid")

    image_reference: str | None = Field(
        default=None,
        description="受控上传目录内的相对图片引用（内容寻址，如 <sha256>.png）",
    )
    record_id: str | None = Field(
        default=None, description="已存在的学习记录 id（服务端反查其受控图片引用）"
    )
    mode: str = Field(default="auto", description="auto | mock | real")
    user_note: str | None = Field(default=None, description="学生自己的备注（可选）")
    subject: str | None = Field(default=None, description="规范学科标签（可选）")

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "StudyOCRSubmitRequest":
        if bool(self.image_reference) == bool(self.record_id):
            raise ValueError("image_reference 与 record_id 必须且只能提供一个")
        if self.mode not in MODES:
            raise ValueError(f"mode 必须是 {list(MODES)} 之一")
        return self


def create_study_ocr_router(adapter: OCRAdapter | None = None) -> APIRouter:
    """工厂：宿主注入共享适配器；默认构造受控存储 + 源项目子进程运行器。"""
    adapter = adapter or OCRAdapter()
    router = APIRouter(prefix="/api/study/ocr", tags=["study-ocr"])

    @router.get("/health")
    def health() -> dict:
        """适配器与 LearningOCR 能力状态（不加载模型、不下载权重）。"""
        return adapter.capabilities()

    @router.post("/uploads")
    async def upload(request: Request, filename: str | None = Query(default=None)) -> dict:
        """把图片按内容寻址写入受控上传目录并返回相对引用。

        ``filename`` 仅作展示元数据；返回的 ``reference`` 为 ``<sha256><后缀>``，
        不含服务器绝对路径，可直接用于 ``POST /jobs``。
        """
        name = filename or request.headers.get("X-Filename") or ""
        body = await request.body()
        try:
            return adapter.save_upload(name, body)
        except OCRPathDenied as exc:
            raise HTTPException(status_code=403, detail={"error": "path_denied",
                                                         "detail": str(exc)})

    @router.post("/jobs")
    def submit(req: StudyOCRSubmitRequest) -> dict:
        """提交处理请求；失败不伪造结果（``status="failed"`` + ``error``）。

        精确重复（同内容 + 同模式且校验通过）幂等复用既有记录，响应 ``reused=true``。
        """
        try:
            return adapter.submit(
                image_reference=req.image_reference, record_id=req.record_id,
                mode=req.mode, user_note=req.user_note, subject=req.subject,
            )
        except OCRPathDenied as exc:
            raise HTTPException(status_code=403, detail={"error": "path_denied",
                                                         "detail": str(exc)})
        except OCRRecordNotFound as exc:
            raise HTTPException(status_code=404, detail={"error": "record_not_found",
                                                         "detail": f"记录不存在: {exc}"})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_request",
                                                         "detail": str(exc)})
        except OCRUnavailable as exc:
            raise HTTPException(status_code=503, detail={"error": "ocr_unavailable",
                                                         "detail": str(exc)})

    @router.get("/jobs")
    def list_jobs(limit: int = Query(50, ge=1, le=200)) -> dict:
        """列出已处理任务（仅元数据；不含图片内容或 OCR 原文）。"""
        return {"jobs": adapter.list_jobs(limit)}

    @router.get("/jobs/{record_id}")
    def get_job(record_id: str, include_audit: bool = Query(True)) -> dict:
        """读取结构化结果：主记录 + 审计 sidecar + 校验 + mock/real 标志。"""
        try:
            return adapter.get_result(record_id, include_audit=include_audit)
        except OCRRecordNotFound as exc:
            raise HTTPException(status_code=404, detail={"error": "record_not_found",
                                                         "detail": f"记录不存在: {exc}"})
        except OCRPathDenied as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid_record_id",
                                                         "detail": str(exc)})

    return router
