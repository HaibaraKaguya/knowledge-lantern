"""Shuchiin Academy — FastAPI Web 服务 + WebSocket 实时进度推送.

══════════════════════════════════════════════════════════════════════
端点总览
══════════════════════════════════════════════════════════════════════
  GET  /              — Web UI (static/index.html)
  GET  /api/health    — 健康检查
  POST /api/run       — 同步运行工作流，阻塞等待完成后返回最终结果
  POST /api/run/stream— 异步启动工作流，立即返回 thread_id，
                         实际进度通过 WebSocket 推送
  GET  /api/sessions  — 列出所有历史会话
  WS   /ws/{thread_id}— WebSocket 连接，接收工作流实时进度事件

══════════════════════════════════════════════════════════════════════
架构：缓冲区模式（Buffer-then-Flush）
══════════════════════════════════════════════════════════════════════

行业标准做法 — 不依赖 HTTP 和 WebSocket 的时序巧合：

  阶段1：任务启动（WebSocket 可能还没连）
    POST /api/run/stream 到达
    → 创建后台任务，事件写入 asyncio.Queue(maxsize=100)
    → _drain() 消费 Queue → broadcast(thread_id, event)
    → broadcast() 发现 _active[thread_id] 为空 → 写入 _buffers[thread_id]
    → HTTP 立即返回 {"thread_id": "..."}

  阶段2：WebSocket 连接（Flush 积压数据）
    客户端 new WebSocket("/ws/{thread_id}")
    → connect() 接受连接
    → 检查 _buffers[thread_id] 是否有积压事件
    → 如果有：一次性全部推送给新客户端（Flush）
    → 清空缓冲区，此后进入实时模式

  阶段3：实时广播
    后续事件到达 broadcast()
    → _active[thread_id] 有连接 → 直接 send_json（实时推送）
    → 不再经过缓冲区

  关键优势：
    - 前端不需要先连 WS 再发 HTTP——任意顺序都行
    - 即使 WS 延迟连接，早期事件也不会丢失
    - 被 Flush 后的事件和实时事件无缝衔接

══════════════════════════════════════════════════════════════════════
进程级任务管理（Task Lifecycle）
══════════════════════════════════════════════════════════════════════

  问题：asyncio.create_task() 创建的任务是"孤儿"——不绑定请求生命周期，
       也不绑定应用生命周期。uvicorn 进程被 kill 时任务直接死。

  解决：
    - 所有后台任务注册到 app.state._tasks（dict[thread_id, Task]）
    - startup 事件：初始化注册表
    - shutdown 事件：遍历所有活跃任务，cancel() 后 await 等待优雅退出
    - 任务完成/异常后自动从注册表移除

  这样后台任务和 FastAPI app 实例同生命周期——app 不退出，任务不会被意外回收。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

# ── 防御 BrokenPipeError：uvicorn reload 时 stdout 管道可能断开，print() 会崩溃 ──
# 用安全包装器静默吞掉 BrokenPipeError，防止工作流被非业务错误中断
class _SafeStdout:
    def __init__(self, stream):
        self._stream = stream
    def write(self, data):
        try: return self._stream.write(data)
        except BrokenPipeError: return 0
    def flush(self):
        try: self._stream.flush()
        except BrokenPipeError: pass
    def __getattr__(self, name):
        return getattr(self._stream, name)
sys.stdout = _SafeStdout(sys.stdout)
sys.stderr = _SafeStdout(sys.stderr)

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.graph.orchestrator import list_sessions as list_sessions_impl
from src.graph.orchestrator import run as run_workflow
from src.graph.orchestrator import run_streaming
from src.utils.files import DATA_DIR

logger = logging.getLogger("shuchiin.server")

# ═══════════════════════════════════════════════════════════════════
# 日志配置：同时输出到控制台和文件（data/shuchiin.log）
# ═══════════════════════════════════════════════════════════════════
_LOG_PATH = os.path.join(DATA_DIR, "shuchiin.log")
_LOGGER_INITIALIZED = False

def _setup_logging():
    global _LOGGER_INITIALIZED
    if _LOGGER_INITIALIZED:
        return
    _LOGGER_INITIALIZED = True
    os.makedirs(DATA_DIR, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # 文件handler（追加模式，保留历史）
    fh = logging.FileHandler(_LOG_PATH, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    # 控制台handler（uvicorn 自带，这里只给我们的 logger 加）
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root = logging.getLogger()
    root.addHandler(fh)
    root.addHandler(ch)
    root.setLevel(logging.INFO)
    # 降低第三方库噪音
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Shuchiin Academy", version="2.1.0")


# ═══════════════════════════════════════════════════════════════════
# CORS 中间件（跨域资源共享）
# ═══════════════════════════════════════════════════════════════════
#
# 为什么需要 CORS？
#   浏览器"同源策略"（Same-Origin Policy）禁止 http://localhost:3000 的 JS
#   请求 http://localhost:8000 的 API。CORS 让服务端声明"哪些来源可以跨域访问我"。
#
# 三个配置项的精确语义：
#   allow_origins      — 哪些 Origin 的页面可以跨域请求我。
#                        设置为具体域名列表，不是 "*"
#   allow_credentials  — 是否允许跨域请求携带 Cookie/Authorization 头。
#                        ⚠️ 设为 True 时 allow_origins 不能是 ["*"]
#   allow_methods      — 允许哪些 HTTP 方法跨域（POST, GET, OPTIONS 等）
#   allow_headers      — 允许哪些非标准请求头跨域
#
# 关于 allow_origins=["*"] + allow_credentials=True 互斥：
#   浏览器 Fetch Standard 3.2.6 规定：当 credentials 模式为 'include' 时，
#   Access-Control-Allow-Origin 不能是通配符 "*"。
#   这是安全措施——防止恶意网站冒用用户的登录态访问你的 API。
#   如果你的前端永远和后端在同一域名下（同源），CORS 不触发，互斥无影响。
#   如果你需要跨域（比如前端部署到 CDN 或开发时用独立 dev server），
#   必须把 allow_origins 改为具体的前端地址列表。

CORS_ORIGINS = os.environ.get(
    "CORS_ORIGINS",
    # 默认值：同源访问（开发环境，前端和后端都在 localhost:8000）
    # 生产环境通过环境变量设置：CORS_ORIGINS="https://app.shuchiin.com,https://admin.shuchiin.com"
    "http://localhost:8000,http://127.0.0.1:8000"
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ORIGINS if o.strip()],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# ═══════════════════════════════════════════════════════════════════
# WebSocket 连接管理器（缓冲区 + Flush + 实时广播）
# ═══════════════════════════════════════════════════════════════════
#
# WebSocket vs HTTP 的区别：
#   HTTP:  请求→响应→结束（或 keep-alive 复用）。客户端拉（pull）。
#   WS:    一次 HTTP Upgrade 握手 → TCP 通道保持打开 → 双向自由发消息。
#          服务端推（push）。适合实时进度、聊天、通知。

class ConnectionManager:
    """按 thread_id 管理 WebSocket 连接池 + 事件缓冲区。

    核心设计：缓冲区模式（Buffer-then-Flush）
      - WS 未连接时：事件写入 _buffers（内存缓冲区）
      - WS 连接瞬间：Flush 缓冲区中所有积压事件
      - WS 连接后：  事件直接 send_json（实时模式）

    这确保了：不管 WebSocket 何时连接（甚至在工作流完成之后），
    所有事件都能被推送给客户端，不会因为时序问题丢失。
    """

    # 缓冲区最大容量（防止内存泄漏：连接永远不来的话事件无限堆积）
    MAX_BUFFER_SIZE = 500

    def __init__(self):
        # {thread_id: [WebSocket, WebSocket, ...]}
        # 一个 thread_id 允许有多个连接（多标签页同时观看）
        self._active: dict[str, list[WebSocket]] = {}
        # {thread_id: [event_dict, event_dict, ...]}
        # WS 未连接时暂存事件，连接后 flush
        self._buffers: dict[str, list[dict]] = {}

    async def connect(self, thread_id: str, ws: WebSocket):
        """接受 WebSocket 连接，并立即 Flush 缓冲区中的积压事件。

        WebSocket 握手流程：
          1. 客户端发 HTTP 请求，带 Upgrade: websocket 头
          2. 服务端 ws.accept() → 返回 101 Switching Protocols
          3. TCP 连接升级为 WebSocket，双向通道打开

        Flush 流程：
          连接建立后立即检查 _buffers[thread_id]：
          - 有积压事件 → 一次性全部推给这个新客户端
          - 清空缓冲区（后续事件走实时广播）
          - 没有积压事件 → 跳过，直接进入实时模式
        """
        await ws.accept()
        # 先注册连接（这样 flush 时如果出错，连接已在列表中）
        self._active.setdefault(thread_id, []).append(ws)

        # ═══════════════════════════════════════════════════════════
        # Flush 阶段：一次性推送所有积压事件
        # ═══════════════════════════════════════════════════════════
        buffered = self._buffers.pop(thread_id, [])
        if buffered:
            logger.info(
                "Flush %d buffered events → WS %s",
                len(buffered), thread_id,
            )
            for event in buffered:
                try:
                    await ws.send_json(event)
                except Exception:
                    # 如果 flush 中途连接断开，停止推送
                    break

    def disconnect(self, thread_id: str, ws: WebSocket):
        """从连接池移除 WebSocket。

        使用 try/except 防范 ws 不在列表中的边缘情况：
        WebSocket 可能因为竞态条件被重复 disconnect。
        """
        try:
            self._active.get(thread_id, []).remove(ws)
        except ValueError:
            pass  # ws 已不在列表中，忽略

    async def broadcast(self, thread_id: str, event: dict):
        """推送事件：有活跃连接→实时广播，无活跃连接→写入缓冲区。

        两阶段逻辑：
          1. 有连接 → send_json 实时推送给所有客户端
          2. 无连接 → append 到 _buffers[thread_id]，
             等待后续 WebSocket 连接时 Flush

        缓冲区保护：
          超过 MAX_BUFFER_SIZE 时丢弃旧事件（FIFO），
          防止连接永远不会来导致的内存泄漏。
        """
        connections = self._active.get(thread_id, [])

        if connections:
            # ── 实时模式：直接推送给所有连接 ──
            dead: list[WebSocket] = []
            for ws in connections:
                try:
                    await ws.send_json(event)
                except Exception:
                    # 连接已死，标记待清理
                    dead.append(ws)
            # 清理死连接（不在遍历时修改列表）
            for ws in dead:
                try:
                    self._active.get(thread_id, []).remove(ws)
                except ValueError:
                    pass
        else:
            # ── 缓冲区模式：暂存事件，等待 WS 连接 ──
            buf = self._buffers.setdefault(thread_id, [])
            buf.append(event)
            # 缓冲区溢出保护：丢弃最旧的事件
            if len(buf) > self.MAX_BUFFER_SIZE:
                dropped = buf.pop(0)
                logger.warning(
                    "Buffer overflow for %s (max=%d), dropped oldest event: %s",
                    thread_id, self.MAX_BUFFER_SIZE, dropped.get("type", "?"),
                )


manager = ConnectionManager()


# ═══════════════════════════════════════════════════════════════════
# 进程级任务管理（绑定到 FastAPI app 生命周期）
# ═══════════════════════════════════════════════════════════════════
#
# 问题：asyncio.create_task() 创建的协程是"孤儿"——
#   - 不和 HTTP 请求的生命周期绑定（请求结束了任务还在跑）
#   - 也不和 app 的生命周期绑定（uvicorn 关闭时任务被强杀，没有清理机会）
#
# 解决：所有后台任务注册到 app.state._tasks 字典中，
#   shutdown 事件遍历 cancel() 所有活跃任务，给它们 5 秒优雅退出时间。
#
# 类比：这就像操作系统的进程表——内核维护所有进程的列表，
# 关机时按顺序发 SIGTERM → 等待 → SIGKILL。

@app.on_event("startup")
async def _startup():
    """应用启动时初始化任务注册表 + 日志配置。"""
    _setup_logging()
    app.state._tasks: dict[str, asyncio.Task[Any]] = {}
    logger.info("Task registry initialized")
    logger.info("Log file: %s", _LOG_PATH)


@app.on_event("shutdown")
async def _shutdown():
    """应用关闭时取消所有活跃后台任务。

    优雅退出流程：
      1. 遍历所有活跃任务，发 cancel()（触发 asyncio.CancelledError）
      2. 给每个任务 5 秒做清理（关闭文件、写最后日志等）
      3. 超时未完成的强制忽略
      4. 清空注册表
    """
    tasks: dict[str, asyncio.Task[Any]] = getattr(app.state, "_tasks", {})
    if not tasks:
        return

    logger.info("Shutting down %d background task(s)...", len(tasks))
    for tid, task in list(tasks.items()):
        task.cancel()

    # 等待所有任务优雅退出（5 秒超时）
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    for tid, result in zip(tasks.keys(), results):
        if isinstance(result, asyncio.CancelledError):
            logger.info("Task %s: cancelled cleanly", tid)
        elif isinstance(result, Exception):
            logger.warning("Task %s: error during shutdown: %s", tid, result)
        else:
            logger.info("Task %s: completed before shutdown", tid)

    tasks.clear()
    logger.info("All background tasks terminated")


def _task_done_callback(thread_id: str):
    """任务完成后的清理回调——从注册表中移除自己。"""
    def _cb(_task: asyncio.Task):
        app.state._tasks.pop(thread_id, None)
    return _cb


# ═══════════════════════════════════════════════════════════════════
# REST API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/pages/{filename:path}")
async def serve_page(filename: str):
    """服务生成的 HTML 页面——可直接在浏览器查看渲染结果."""
    pages_dir = os.path.join(DATA_DIR, "pages")
    file_path = os.path.join(pages_dir, filename)
    if not os.path.isfile(file_path):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Page not found"}, status_code=404)
    return FileResponse(file_path, media_type="text/html")


@app.get("/api/skills")
async def api_skills():
    """返回所有已安装 skill 的 name + description，供前端 / 补全使用."""
    from src.skills import load_skills, get_skill_names
    skills = load_skills()
    return {
        "skills": [{"name": s.name, "description": s.description} for s in skills],
        "names": get_skill_names(skills),
    }


@app.get("/api/constitution")
async def api_get_constitution():
    """读取用户宪法文件内容."""
    import yaml
    from src.constitution import CONSTITUTION_PATH, load_constitution
    try:
        with open(CONSTITUTION_PATH, "r", encoding="utf-8") as f:
            raw = f.read()
        return {"content": raw, "parsed": load_constitution()}
    except FileNotFoundError:
        return {"content": "", "parsed": load_constitution()}


@app.post("/api/constitution")
async def api_save_constitution(data: dict[str, Any]):
    """保存用户宪法文件."""
    import yaml
    from src.constitution import CONSTITUTION_PATH
    content = data.get("content", "")
    if not content or not content.strip():
        return {"error": "content 不能为空"}, 400
    try:
        yaml.safe_load(content)  # 验证 YAML 语法
    except yaml.YAMLError as e:
        return {"error": f"YAML 语法错误: {e}"}, 400
    os.makedirs(os.path.dirname(CONSTITUTION_PATH), exist_ok=True)
    with open(CONSTITUTION_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    return {"status": "ok", "path": CONSTITUTION_PATH}


@app.get("/api/health")
async def health():
    """健康检查。负载均衡器/监控系统定期探测此端点。"""
    return {"status": "ok", "service": "Shuchiin Academy"}


@app.post("/api/run")
async def api_run(task: dict[str, Any]):
    """同步运行工作流。

    请求体：
      {"user_input": "开发一个登录页面", "thread_id": "可选"}

    行为：
      - 阻塞等待工作流执行完毕（可能几十秒）
      - HTTP 连接在此期间保持打开
      - 适合 CLI / 脚本调用，不适合浏览器（长时间等待无进度反馈）

    虽然是 async def，但 run_workflow() 是同步函数——
    FastAPI 自动检测并用 run_in_threadpool 放到线程池执行，
    不会阻塞事件循环中的其他请求。
    """
    user_input = task.get("user_input", "")
    thread_id = task.get("thread_id") or f"session-{time.strftime('%Y%m%d-%H%M%S')}"

    try:
        result = run_workflow(user_input, thread_id=thread_id)
        ledger = result.get("task_ledger", [])
        done = sum(1 for t in ledger if t.get("status") == "done")
        return {
            "thread_id": thread_id,
            "task_ledger": ledger,
            "done": done,
            "total": len(ledger),
            "approved": result.get("approved", False),
            "direct_answer": result.get("direct_answer", ""),
        }
    except Exception as e:
        return {"error": str(e), "thread_id": thread_id}


@app.get("/api/sessions")
async def api_sessions():
    """列出所有历史会话（从 SQLite checkpoint 读取）。"""
    sessions = list_sessions_impl()
    return {"sessions": sessions}


@app.get("/api/sessions/{thread_id}/history")
async def api_session_history(thread_id: str):
    """读取单个会话的对话历史（从 SQLite checkpoint 读取）。

    对同一 thread 内可能重复保存的父图 checkpoint 做去重：
    只保留每个 user_input 对应的最新一轮（checkpoint_id 最大）。
    """
    from src.utils.files import DATA_DIR
    import sqlite3, msgpack
    from collections import OrderedDict

    db_path = os.path.join(DATA_DIR, "checkpoints.db")
    if not os.path.exists(db_path):
        return {"messages": []}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT checkpoint_id, checkpoint FROM checkpoints "
            "WHERE thread_id = ? ORDER BY checkpoint_id",
            (thread_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"messages": []}
    finally:
        conn.close()

    # 提取候选：按 (user_input, latest_checkpoint_id) 去重
    candidates: OrderedDict[str, dict] = OrderedDict()
    for r in rows:
        try:
            d = msgpack.unpackb(r["checkpoint"], raw=False)
            cv = d.get("channel_values", {})
        except Exception:
            continue
        user_input = cv.get("user_input")
        if not user_input:
            continue
        key = str(user_input)
        # 后面的 checkpoint_id 更大，覆盖前面的
        candidates[key] = {
            "user_input": str(user_input),
            "checkpoint_id": r["checkpoint_id"],
            "direct_answer": cv.get("direct_answer"),
            "task_ledger": cv.get("task_ledger") or [],
            "approved": cv.get("approved", False),
        }

    messages = []
    for c in candidates.values():
        messages.append({
            "role": "user",
            "content": c["user_input"],
            "checkpoint_id": c["checkpoint_id"],
        })

        direct = c["direct_answer"]
        if direct:
            messages.append({
                "role": "assistant",
                "content": str(direct),
                "checkpoint_id": c["checkpoint_id"],
            })
        else:
            ledger = c["task_ledger"]
            if ledger:
                done = sum(1 for t in ledger if t.get("status") == "done")
                total = len(ledger)
                approved = c["approved"]
                lines = [
                    f"工作流完成: {done}/{total} 任务完成",
                    f"审批: {'通过' if approved else '未通过'}",
                    "",
                    "任务列表:",
                ]
                for t in ledger:
                    status = t.get("status", "?")
                    desc = t.get("description") or t.get("summary") or t.get("id", "")
                    assignee = t.get("assignee", "")
                    lines.append(f"  [{status}] {assignee}: {desc}")
                messages.append({
                    "role": "assistant",
                    "content": "\n".join(lines),
                    "checkpoint_id": c["checkpoint_id"],
                })

    return {"messages": messages}


@app.post("/api/run/stream")
async def api_run_stream(task: dict[str, Any]):
    """异步启动工作流，通过 WebSocket 推送实时进度。

    ═══════════════════════════════════════════════════════════════
    工作流（事件不丢、任意时序、背压保护）
    ═══════════════════════════════════════════════════════════════

    ┌─────────────────────────────────────────────────────────────┐
    │  POST /api/run/stream                                       │
    │  │                                                          │
    │  ├─ _run()  ──→  asyncio.Queue(maxsize=100)  ──→  _drain() │
    │  │   (生产者)       ↑背压：满了阻塞put       (消费者)       │
    │  │                  │                                       │
    │  │                  │  await queue.put(event)                │
    │  │                  │  如果队列满→await阻塞→等待消费        │
    │  │                  │                                       │
    │  └─ 两个 create_task 注册到 app.state._tasks               │
    │      进程退出时 shutdown 事件会 cancel 它们                  │
    └─────────────────────────────────────────────────────────────┘
              │                              │
              │  _drain() 调用               │
              │  manager.broadcast()         │
              │                              │
              ▼                              ▼
    ┌─────────────────┐        ┌─────────────────────────────┐
    │  _buffers 缓冲区  │        │  _active WebSocket 连接      │
    │  (WS 未连时暂存) │  ──→  │  (实时 send_json)            │
    │                  │ Flush │                              │
    └─────────────────┘        └─────────────────────────────┘
    """
    user_input = task.get("user_input", "")
    thread_id = task.get("thread_id") or f"session-{time.strftime('%Y%m%d-%H%M%S')}"

    # asyncio.Queue(maxsize=100) — 背压保护：
    #   队列满时 await queue.put() 会阻塞，暂停工作流的事件生产，
    #   直到 _drain() 消费掉一些事件腾出空间。
    #   100 对于典型工作流（~30-60 个事件）足够，且提供了安全余量。
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)

    async def _run():
        """后台任务1（生产者）：运行 LangGraph 工作流，事件写入 queue。"""
        try:
            await run_streaming(user_input, thread_id=thread_id, event_queue=queue)
        except asyncio.CancelledError:
            # 进程关闭时的优雅退出——不记录为错误
            logger.info("Workflow %s cancelled (shutdown)", thread_id)
        except Exception as e:
            # 记录完整 traceback 到服务端日志
            logger.error("Workflow %s failed: %s", thread_id, e, exc_info=True)
            # 工作流异常也作为事件推送给前端（包含 traceback）
            try:
                import traceback as _tb
                detail = _tb.format_exc()
            except Exception:
                detail = str(e)
            await queue.put({"type": "error", "data": f"{e}\n---\n{detail}"})

    async def _drain():
        """后台任务2（消费者）：从 queue 取事件 → 广播。

        通过 manager.broadcast() 分发：
          - WS 已连接 → send_json 实时推送
          - WS 未连接 → 写入 _buffers 等待 Flush

        退出条件：收到 "workflow_complete" 或 "error" 事件。
        """
        try:
            while True:
                event = await queue.get()
                await manager.broadcast(thread_id, event)
                if event["type"] in ("workflow_complete", "error"):
                    break
        except asyncio.CancelledError:
            logger.info("Drain %s cancelled (shutdown)", thread_id)

    # ── 进程级任务注册 ──
    # 任务存入 app.state._tasks[thread_id]，shutdown 事件可以取消它们。
    # 任务完成或异常后自动从注册表移除（通过 add_done_callback）。
    t_run = asyncio.create_task(_run())
    t_drain = asyncio.create_task(_drain())

    # 多个任务共享同一个 thread_id slot——我们用 sub-key 区分
    app.state._tasks[f"{thread_id}:run"] = t_run
    app.state._tasks[f"{thread_id}:drain"] = t_drain

    # 两个任务都完成后清理注册表
    async def _cleanup():
        await asyncio.gather(t_run, t_drain, return_exceptions=True)
        app.state._tasks.pop(f"{thread_id}:run", None)
        app.state._tasks.pop(f"{thread_id}:drain", None)

    asyncio.create_task(_cleanup())

    # HTTP 响应立即返回，不等工作流完成
    return {"thread_id": thread_id}


# ═══════════════════════════════════════════════════════════════════
# WebSocket 端点
# ═══════════════════════════════════════════════════════════════════

@app.websocket("/ws/{thread_id}")
async def ws_endpoint(ws: WebSocket, thread_id: str):
    """WebSocket 端点 — 接收工作流实时进度事件。

    客户端使用方式（JavaScript）：
      const ws = new WebSocket(`ws://localhost:8000/ws/${threadId}`);
      ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        // data.type: "node_start" | "task_created" | "task_done" |
        //            "review" | "task_dispatch" | "workflow_complete" | "error"
      };

    生命周期：
      1. new WebSocket(url) → TCP 连接 + HTTP Upgrade 握手
      2. connect() → accept() + Flush 缓冲区积压事件
      3. 实时阶段：broadcast() 直接 send_json
      4. 客户端关闭 → WebSocketDisconnect → disconnect() 清理
    """
    await manager.connect(thread_id, ws)
    try:
        while True:
            # 阻塞等待客户端消息——我们不需要消息内容，
            # 只是用这个循环保持连接活跃 + 检测断开。
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(thread_id, ws)


# ═══════════════════════════════════════════════════════════════════
# 静态文件
# ═══════════════════════════════════════════════════════════════════

@app.get("/")
async def root():
    """Web UI 入口 — 返回 static/index.html。"""
    return FileResponse(STATIC_DIR / "index.html")


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ═══════════════════════════════════════════════════════════════════
# 启动入口
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    uvicorn.run("src.server:app", host="0.0.0.0", port=8000, reload=True)
