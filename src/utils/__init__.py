"""Utils 包 — Agent 间通信的 JSON 文件读写.

save_json  — 保存产出到 data/outputs/ 或 data/research/，返回路径
load_json  — 读取 JSON 产出文件
ensure_dirs— 确保 data/outputs/ 和 data/research/ 目录存在
summarize  — 从 JSON 文件提取摘要（不加载全文到 state）
"""
from src.utils.files import save_json, load_json, ensure_dirs
__all__ = ["save_json", "load_json", "ensure_dirs"]
