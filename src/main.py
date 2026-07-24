#!/usr/bin/env python3
"""Shuchiin Academy CLI — 命令行入口.

解析用户输入，调用 orchestrator.run() 执行工作流，打印任务结果摘要。
支持 --thread-id 继续对话、--list 列出历史会话。
"""

from __future__ import annotations
import sys, argparse
# 禁用输出缓冲——实时看到 print 输出，不用等程序结束
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None
sys.stderr.reconfigure(line_buffering=True) if hasattr(sys.stderr, 'reconfigure') else None

def main():
    p = argparse.ArgumentParser(description="Shuchiin Academy 动态工作流")
    p.add_argument("task", nargs="?", default="开发一个Web计算器")
    p.add_argument("--thread-id", "-t", default=None, help="对话线程ID")
    p.add_argument("--list", "-l", action="store_true", help="列出所有对话")
    args = p.parse_args()

    from src.graph.orchestrator import run, list_sessions, continue_conversation
    from src.config import settings

    if args.list:
        sessions = list_sessions()
        if not sessions: print("无历史对话"); return
        for s in sessions: print(f"  {s['thread_id']} (steps={s['steps']})")
        return

    user_input = args.task
    print(f"╔══════════════════════════════════════════════╗")
    print(f"║  Shuchiin Academy · CEO-Dev-Researcher-Reviewer ║")
    print(f"╚══════════════════════════════════════════════╝")
    print(f"\n📋 任务: {user_input}\n🤖 模型: {settings.model}\n🔄 最大重试: 3\n")

    if args.thread_id:
        result = continue_conversation(args.thread_id, user_input)
    else:
        result = run(user_input)

    print(f"\n{'='*50}")
    ledger = result.get("task_ledger", [])
    done = sum(1 for t in ledger if t["status"] == "done")
    print(f"  完成任务: {done}/{len(ledger)}")
    for t in ledger:
        icon = "✅" if t["status"] == "done" else "⏳" if t["status"] == "pending" else "🚫"
        print(f"  {icon} [{t['id']}] {t['assignee']}: {str(t.get('description',''))[:60]}")

    # ── 实际产出 ──
    review = result.get("review_report") or {}
    score = review.get("score", "?")
    print(f"\n📊 Review 评分: {score}")

    html_url = result.get("html_url", "")
    if html_url:
        print(f"🌐 预览页面: {html_url}")

    output_path = result.get("output_path", "")
    if output_path:
        print(f"📁 产出文件: {output_path}")

    # 调研/报告类：直接打印内容
    outputs = result.get("outputs") or {}
    for role, data in outputs.items():
        content = data.get("content", "")
        content_type = data.get("content_type", "")
        if content and content_type in ("research", "report"):
            print(f"\n📄 [{role}] 产出内容:")
            print(content[:3000])
            if len(content) > 3000:
                print(f"\n... (共 {len(content)} 字符，已截断)")

    print(f"\n🔄 重试: {result.get('retry_count',0)}/{result.get('max_retries',3)}")

if __name__ == "__main__": main()
