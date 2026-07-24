"""Agent 包 — 四个核心 Agent + LLM 封装.

ceo       — 意图分类 + 任务拆解 + 反馈路由（纯代码路由，非 LLM）
developer — 子图隔离的代码生产者（write → self_check → save）
researcher— 子图隔离的知识调研员（research → save）
reviewer  — 双维度审查官（knowledge_gap / code_quality）
llm       — ChatOpenAI 封装 + JSON 安全解析
"""
