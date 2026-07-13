"""ai-radar：AI 前沿信息收集与摘要 agent。

流水线全貌（LangGraph 编排，节点为纯函数）：
    采集(collectors) → 去重 → 打分 → 深读(llm) → 关联历史 → 校验(validate) → 落盘/推送(report)

设计依据见 docs/设计方案-协商纪要-2026-07-12.md，施工顺序见 docs/施工计划.md。
"""
