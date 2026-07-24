"""SkillRunner — 执行 Skill 脚本的 Agent.

输入: {"skill_name": "...", "user_input": "...", "research_notes": "..."}
输出: {"final_code": "...", "output_path": "..."}

流程:
  1. 加载 skill 的 SKILL.md → 读 Markdown 正文（完整指令）
  2. LLM 根据指令 + 用户输入 → 生成脚本参数
  3. 执行脚本（subprocess 或 Docker）
  4. 保存结果 → END
"""

from __future__ import annotations
import json as _json
import os as _os
import re as _re
import subprocess
import tempfile
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from src.agents.llm import get_llm, safe_parse_json
from src.skills import load_skills
from src.state.types import DeveloperState  # 复用 DeveloperState 字段
from src.utils.files import save_json, DATA_DIR

# Skills 列表在启动时加载一次（避免每次调用都扫描磁盘）
_SKILL_CACHE: dict[str, object] | None = None


def _get_skill(name: str):
    """按名称查找 skill（带缓存）."""
    global _SKILL_CACHE
    if _SKILL_CACHE is None:
        skills = load_skills()
        _SKILL_CACHE = {s.name: s for s in skills}
    return _SKILL_CACHE.get(name)


def _list_script_skills() -> list[dict]:
    """列出所有可执行脚本的 skill."""
    global _SKILL_CACHE
    if _SKILL_CACHE is None:
        skills = load_skills()
        _SKILL_CACHE = {s.name: s for s in skills}
    result = []
    for s in _SKILL_CACHE.values():
        if s.executor == "script":
            result.append({
                "name": s.name,
                "description": s.description,
                "runtime": s.runtime,
                "entrypoint": s.entrypoint,
            })
    return result


PARAM_GEN_PROMPT = """\
你是技能参数生成器。根据技能指令和用户需求，生成调用脚本所需的参数。

## 可用技能信息
{skill_instructions}

## 用户需求
{user_input}

## 规则
- 输出严格 JSON 格式
- 如果技能指令中指定了 CLI 参数格式，严格遵循
- 如果技能只是 prompt 类型（无脚本），返回 type=prompt，把完整 prompt 放在 content 字段

## 输出 JSON
{{
  "type": "script",
  "args": ["--param1", "value1", "--param2", "value2"],
  "env_vars": {{"KEY": "value"}},
  "input_prompt": "如果脚本需要读 prompt 文件，这里写 prompt 内容",
  "output_file": "预期的输出文件名"
}}

如果不知道该怎么填参数，type 设为 "unknown"。
"""


def _plan(state: DeveloperState, llm=None) -> dict:
    """LLM 分析 skill 指令 + 用户输入 → 生成执行参数."""
    llm = llm or get_llm(0.2)
    skill_name = state.get("task_description", "")
    user_input = state.get("role_prompt", "")  # 偷懒：用 role_prompt 字段传原始用户输入
    research = state.get("research_notes", "")

    skill = _get_skill(skill_name) if skill_name else None
    if not skill:
        return {"_exec_params": {"type": "unknown", "error": f"Skill '{skill_name}' 未找到"}}

    instructions = skill.prompt_instructions
    context = user_input or research or skill_name

    try:
        resp = llm.invoke([
            SystemMessage(content=PARAM_GEN_PROMPT.format(
                skill_instructions=instructions[:4000],
                user_input=context[:1000],
            )),
            HumanMessage(content=f"为技能 {skill_name} 生成执行参数"),
        ]).content
        params = safe_parse_json(resp, {"type": "unknown", "error": "LLM 返回无效 JSON"})
    except Exception as e:
        params = {"type": "unknown", "error": str(e)}

    print(f"     🎯 SkillRunner [{skill_name}]: type={params.get('type')}, "
          f"executor={skill.executor}, runtime={skill.runtime}")

    return {
        "_exec_params": params,
        "_skill_name": skill_name,
        "_events": [],
    }


def _execute_script(state: DeveloperState) -> dict:
    """执行 skill（prompt 类型=LLM，script 类型=沙箱）."""
    params = state.get("_exec_params", {})
    skill_name = state.get("_skill_name", "")
    skill = _get_skill(skill_name)

    if not skill:
        error_msg = f"Skill '{skill_name}' 未找到"
        print(f"     ❌ {error_msg}")
        return {"final_code": error_msg, "output_path": "",
                "_events": [{"type": "error", "data": error_msg}]}

    # ── Prompt 类型：LLM 直接执行指令 ──
    if skill.executor == "prompt":
        return _execute_prompt(skill, params)

    # ── Script 类型：沙箱执行脚本 ──
    if not skill.script_path or not skill.script_path.exists():
        error_msg = f"脚本不存在: {skill.entrypoint if skill else '?'}"
        print(f"     ❌ {error_msg}")
        return {"final_code": error_msg, "output_path": "",
                "_events": [{"type": "error", "data": error_msg}]}

    return _execute_script_sandboxed(skill, params)


def _execute_prompt(skill, params: dict) -> dict:
    """Prompt 类型 skill：LLM 读 Markdown 指令 → 直接产出结果."""
    from src.agents.llm import get_llm
    llm = get_llm(0.3)

    user_input = params.get("user_input", "")
    instructions = skill.prompt_instructions[:6000]

    system = SystemMessage(content=f"""你是技能执行者。严格按照以下技能指令完成任务。

## 技能指令
{instructions}

## 规则
- 严格遵循指令中的工作流
- 产出完整的最终结果
- 如果需要搜索/调研，基于你的知识给出最准确的答案""")
    human = HumanMessage(content=user_input or f"执行技能 {skill.name}")

    try:
        resp = llm.invoke([system, human]).content
        print(f"     📝 Prompt Skill [{skill.name}]: {len(resp)} 字符")
    except Exception as e:
        resp = f"执行失败: {e}"

    return {
        "final_code": resp[:10000],
        "output_path": "",
        "_events": [{"type": "file_save", "total_chars": len(resp), "file_count": 1, "html_url": ""}],
    }


def _execute_script_sandboxed(skill, params: dict) -> dict:
    script = str(skill.script_path)
    runtime = skill.runtime or "python"
    args = params.get("args", [])

    # 写入 prompt 文件
    prompt_file = None
    input_prompt = params.get("input_prompt", "")
    if input_prompt:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        tmp.write(input_prompt)
        tmp.close()
        prompt_file = tmp.name

    cmd = _build_command(runtime, script, args, prompt_file or "")

    # ── 沙箱执行：Docker 优先，subprocess 降级 ──
    from src.sandbox import run_in_sandbox as _sandbox_run
    from src.sandbox.executor import _docker_available

    if _docker_available():
        result = _run_skill_in_docker(cmd, skill, params)
    else:
        result = _run_skill_subprocess(cmd, skill, params, prompt_file)

    if prompt_file and _os.path.exists(prompt_file):
        _os.unlink(prompt_file)

    output = result.get("output", "")
    passed = result.get("passed", False)
    sandbox_type = result.get("sandbox_type", "?")

    status = "✅" if passed else "❌"
    print(f"     📦 Skill Sandbox({sandbox_type}): {status} ({result.get('duration_ms', 0)}ms)")
    if not passed:
        print(f"     {output[:200]}")

    return {
        "final_code": output[:10000],
        "output_path": "",
        "_events": [{"type": "file_save", "total_chars": len(output),
                      "file_count": 1, "html_url": ""}],
    }


def _run_skill_in_docker(cmd: list[str], skill, params: dict) -> dict:
    """Docker 容器里执行技能脚本（允许网络）."""
    import time as _time
    start = _time.monotonic()
    skill_dir = str(skill.skill_dir)

    docker_cmd = [
        "docker", "run", "--rm",
        "--memory=512m", "--cpus=1",
        "--pids-limit=64",
        "-v", f"{skill_dir}:/skill:ro",      # skill 目录只读
        "-v", f"/tmp:/tmp:rw",                  # /tmp 可写（输出文件）
        f"--name=skill-{_os.getpid()}-{int(_time.time())}",
    ]

    # 选择镜像
    runtime = skill.runtime or "python"
    image_map = {"python": "python:3.12-alpine", "node": "node:20-alpine", "bash": "alpine:latest"}
    image = image_map.get(runtime, "python:3.12-alpine")

    # 把 cmd 中的本地路径替换为容器内路径
    container_cmd = []
    for c in cmd:
        c = c.replace(skill_dir, "/skill")
        c = c.replace(tempfile.gettempdir(), "/tmp")
        container_cmd.append(c)

    docker_cmd.append(image)
    docker_cmd.extend(container_cmd)

    print(f"     🐳 Docker: {' '.join(docker_cmd[:6])}...")

    try:
        env = {k: v for k, v in (_os.environ).items()
               if k.startswith(("GEMINI", "MINIMAX", "GITHUB", "OPENAI", "TAVILY"))}
        for k, v in (params.get("env_vars") or {}).items():
            env[k] = v
        env_args = []
        for k, v in env.items():
            env_args.extend(["-e", f"{k}={v}"])
        # 插入 env 到 docker_cmd 中（在 image 之前）
        # docker_cmd = ["docker","run","--rm",..., image, cmd...]
        # env 需要插在 image 之前
        image_idx = docker_cmd.index(image)
        docker_cmd = docker_cmd[:image_idx] + env_args + docker_cmd[image_idx:]

        result = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=120)
        output = result.stdout + "\n" + result.stderr
        return {
            "passed": result.returncode == 0,
            "output": output[:5000],
            "sandbox_type": "docker",
            "duration_ms": int((_time.monotonic() - start) * 1000),
        }
    except subprocess.TimeoutExpired:
        _cleanup_docker(f"skill-{_os.getpid()}")
        return {"passed": False, "output": "Docker 执行超时 (120s)", "sandbox_type": "docker", "duration_ms": 0}
    except Exception as e:
        return {"passed": False, "output": str(e), "sandbox_type": "docker", "duration_ms": 0}


def _run_skill_subprocess(cmd: list[str], skill, params: dict, prompt_file: str | None) -> dict:
    """subprocess 降级执行（带安全限制）."""
    import time as _time
    start = _time.monotonic()

    print(f"     🔧 subprocess: {' '.join(cmd[:6])}...")

    try:
        env = {**_os.environ, **(params.get("env_vars") or {})}
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=120,
            cwd=str(skill.skill_dir),
            env=env,
            # 安全限制
            preexec_fn=_os.setpgrp if hasattr(_os, 'setpgrp') else None,  # 进程组隔离
        )
        output = result.stdout + "\n" + result.stderr
        return {
            "passed": result.returncode == 0,
            "output": output[:5000],
            "sandbox_type": "subprocess",
            "duration_ms": int((_time.monotonic() - start) * 1000),
        }
    except subprocess.TimeoutExpired:
        return {"passed": False, "output": "执行超时 (120s)", "sandbox_type": "subprocess", "duration_ms": 0}
    except Exception as e:
        return {"passed": False, "output": str(e), "sandbox_type": "subprocess", "duration_ms": 0}


def _cleanup_docker(prefix: str):
    """清理残留 Docker 容器."""
    try:
        r = subprocess.run(["docker", "ps", "-q", "--filter", f"name={prefix}"],
                           capture_output=True, text=True, timeout=5)
        for cid in r.stdout.strip().split("\n"):
            if cid:
                subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=5)
    except Exception:
        pass


def _build_command(runtime: str, script: str, args: list[str], prompt_file: str) -> list[str]:
    """构建执行命令."""
    if runtime == "python":
        cmd = ["python3", script]
    elif runtime == "node":
        cmd = ["node", script]
    elif runtime == "bash":
        cmd = ["bash", script]
    else:
        cmd = ["python3", script]  # fallback

    # 注入 prompt-file 参数（脚本通常有 --prompt-file 参数）
    if prompt_file:
        args = [a for a in args if not a.startswith("--prompt-file")]
        cmd.extend(["--prompt-file", prompt_file])

    # 注入 output-file 参数（确保输出到 data/ 目录）
    args = [a for a in args if not a.startswith("--output-file")]
    _os.makedirs(DATA_DIR, exist_ok=True)
    output_file = str(Path(DATA_DIR) / "skill_output")
    cmd.extend(["--output-file", output_file])

    cmd.extend(args)
    return cmd


def _save(state: DeveloperState) -> dict:
    """保存执行结果."""
    data = {
        "skill": state.get("_skill_name", ""),
        "output": state.get("final_code", ""),
        "params": state.get("_exec_params", {}),
    }
    path = save_json("outputs", "skill", "SK", data)
    print(f"     💾 技能结果保存到: {path}")
    return {"output_path": path, "final_code": state.get("final_code", "")}


def build_skill_runner_subgraph(llm=None) -> StateGraph:
    """构建 SkillRunner 子图: _plan → _execute_script → _save → END."""
    llm = llm or get_llm()
    g = StateGraph(DeveloperState)
    g.add_node("plan", lambda s: _plan(s, llm))
    g.add_node("execute", _execute_script)
    g.add_node("save", _save)
    g.set_entry_point("plan")
    g.add_edge("plan", "execute")
    g.add_edge("execute", "save")
    g.add_edge("save", END)
    return g.compile()
