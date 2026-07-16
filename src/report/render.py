"""周中报渲染：一次生成三份产物。

- markdown（人读）：三板块深读/中读/速览 + 尾注
- YAML frontmatter（机器读）：ReportMeta，P6 进实体索引
- JSON sidecar（周报的输入）：全量条目结构化数据，周日周报只读它不重新采集

Cite 纪律（设计纪要第 5 节）：所有 URL 从 NewsItem 带入，模型产出的 analysis
里不含链接——引用行为发生在这里，不发生在模型里。
"""

import yaml

from src.models import Category, NewsItem, ReportMeta, RunType
from src.report.select import Selection

CATEGORY_NAMES = {
    Category.MODEL: "模型动态",
    Category.ENGINEERING: "工程实践",
    Category.PAPER: "论文·新技术",
}
GLANCE_DISPLAY_CAP = 15  # 速览每板块最多展示条数：首轮存量洪峰时防报告爆炸，全量在 JSON


def _cite_line(item: NewsItem) -> str:
    entities = f" · 实体：{'、'.join(item.entities)}" if item.entities else ""
    return (
        f"> 评分 {item.score}/10 · {item.score_reason}\n"
        f"> 来源：<{item.url}> · 发布 {item.published_at.date()}{entities}\n"
    )


def _item_block(item: NewsItem, marker: str) -> str:
    # 分析失败的降级路径：摘要+打分理由顶上，并如实标注（报告不说谎）
    body = item.analysis or f"（深读生成失败，以下为原文摘要）\n\n{item.summary[:500]}"
    return f"### {marker} {item.title}\n\n{body}\n\n{_cite_line(item)}\n"


def _glance_line(item: NewsItem) -> str:
    note = item.score_reason or item.summary[:80]
    score = f"{item.score}分 · " if item.score is not None else ""
    return f"- [{item.title}]({item.url}) — {score}{note}"


def render_midweek(
    selection: Selection,
    failures: dict[str, str],
    raw_count: int,
    usage: tuple[int, int, float],  # (入 tokens, 出 tokens, 成本上限元)
    date_str: str,
    source_orgs: list[str] | None = None,  # 本期采集成功的机构名（按配置顺序去重）
) -> tuple[str, ReportMeta]:
    deep_by_cat = {i.category: i for i in selection.deep}
    mid_by_cat = {i.category: i for i in selection.mid}

    sections: list[str] = []
    for cat in Category:
        parts: list[str] = []
        if deep := deep_by_cat.get(cat):
            parts.append(_item_block(deep, "⭐ 深读 |"))
        if mid := mid_by_cat.get(cat):
            parts.append(_item_block(mid, "中读 |"))
        glance = selection.glance.get(cat, [])
        if glance:
            lines = [_glance_line(i) for i in glance[:GLANCE_DISPLAY_CAP]]
            if len(glance) > GLANCE_DISPLAY_CAP:
                lines.append(f"- …另有 {len(glance) - GLANCE_DISPLAY_CAP} 条见同名 JSON")
            parts.append("**速览**\n\n" + "\n".join(lines) + "\n")
        if parts:
            sections.append(f"## {CATEGORY_NAMES[cat]}\n\n" + "\n".join(parts))

    if selection.unscored:
        lines = [_glance_line(i) for i in selection.unscored[:GLANCE_DISPLAY_CAP]]
        sections.append("## 未打分条目\n\n（本批打分失败，保留待查）\n\n" + "\n".join(lines) + "\n")

    # 尾注：来源声明 + 缺失源 + 统计 + 成本实测（设计纪要第 11、15 节）
    footer = ["## 尾注\n"]
    if source_orgs:
        footer.append(f"- 本期资源收集自：{'、'.join(source_orgs)}")
    if failures:
        footer.append("**本期缺失源**\n")
        footer.extend(f"- {sid}：{reason}" for sid, reason in failures.items())
        footer.append("")
    else:
        # 显式声明而不是留白：读者要能区分「没失败」和「忘了写」
        footer.append("- 本期全部源采集正常")
    scored_count = len(selection.deep) + len(selection.mid) + sum(
        len(v) for v in selection.glance.values()
    )
    tokens_in, tokens_out, cost = usage
    footer.append(
        f"- 条目：原始 {raw_count} → 新增 {scored_count + len(selection.unscored)}"
        f"（打分 {scored_count}，未打分 {len(selection.unscored)}）"
    )
    footer.append(
        f"- 成本：入 {tokens_in:,} / 出 {tokens_out:,} tokens ≈ ¥{cost:.2f}"
        f"（按 pro 牌价上限估，P5 接观测后有精确值）"
    )

    all_entities = sorted({e for i in selection.deep + selection.mid for e in i.entities})
    meta = ReportMeta(
        date=date_str,
        run_type=RunType.MIDWEEK,
        description="深读：" + "；".join(i.title for i in selection.deep)[:200],
        entities=all_entities,
        tags=[c.value for c in Category if c in deep_by_cat],
        item_count=scored_count + len(selection.unscored),
        sources_failed=[f"{sid}：{reason}" for sid, reason in failures.items()],
        tokens_used=tokens_in + tokens_out,
        cost_cny=round(cost, 4),
    )

    frontmatter = yaml.safe_dump(
        meta.model_dump(mode="json"), allow_unicode=True, sort_keys=False
    ).strip()
    md = (
        f"---\n{frontmatter}\n---\n\n# AI 前沿周中报 · {date_str}\n\n"
        + "\n".join(sections)
        + "\n"
        + "\n".join(footer)
        + "\n"
    )
    return md, meta


def report_payload(selection: Selection, meta: ReportMeta) -> dict:
    """JSON sidecar：周报（P6）的唯一输入。选题结果与全量条目都在。"""

    def dump(items: list[NewsItem]) -> list[dict]:
        return [i.model_dump(mode="json") for i in items]

    return {
        "meta": meta.model_dump(mode="json"),
        "deep": dump(selection.deep),
        "mid": dump(selection.mid),
        "glance": {c.value: dump(v) for c, v in selection.glance.items()},
        "unscored": dump(selection.unscored),
    }
