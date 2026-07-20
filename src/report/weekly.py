"""周报：读本周周中报 JSON 做层级摘要，不重新采集（设计纪要第 6、8 节定案）。

结构四段：Top5（跨板块全局视角，与周中报的板块配额制互补）、趋势段（LLM 生成，
可含历史演进）、分板块一览（题录级）、面试视角（LLM 生成）。趋势段/面试视角
是周报唯一的新生成内容，其余全部来自周中报已有产物——周报的钱只花在综合上。
"""

import re
from dataclasses import dataclass

import yaml

from src.models import Category, NewsItem, ReportMeta, RunType, dedup_key
from src.report.render import CATEGORY_NAMES, RunUsage

TOP_N = 5
MIN_TOP_SCORE = 7  # 周报门面：不够深读线以上的条目不配进 Top5
GLANCE_CAP = 12  # 一览每板块上限：题录级信息，超出部分周中报里都有
BLURB_CHARS = 220  # Top5 简介取分析首段截断：细节读周中报，周报只给「为什么重要」


@dataclass(frozen=True)
class WeeklySections:
    """LLM 生成的两段。None = 生成失败，渲染层如实标注（报告不说谎）。"""

    trend: str | None
    interview: str | None


_TREND = re.compile(r"【趋势】\s*(.+?)(?=【面试视角】|\Z)", re.S)
_INTERVIEW = re.compile(r"【面试视角】\s*(.+?)\Z", re.S)


def parse_sections(text: str) -> WeeklySections:
    trend = _TREND.search(text)
    interview = _INTERVIEW.search(text)
    return WeeklySections(
        trend=trend.group(1).strip() if trend else None,
        interview=interview.group(1).strip() if interview else None,
    )


def week_pool(payloads: list[dict]) -> list[NewsItem]:
    """合并本周各期周中报的全量条目去重（回填与常规运行、跨源同文都可能重叠）。

    按 dedup_key(url) 而非 id 去重：能收敛同一 arXiv 论文的 arxiv/HF 两个入口，
    也对历史数据里 id 不一致的同文条目稳健（防御 make_item_id 升级前的旧数据）。
    """
    seen: set[str] = set()
    pool: list[NewsItem] = []
    for payload in payloads:
        buckets = [payload["deep"], payload["mid"], *payload["glance"].values()]
        for raw in (item for bucket in buckets for item in bucket):
            key = dedup_key(raw["url"])
            if key in seen:
                continue
            seen.add(key)
            pool.append(NewsItem.model_validate(raw))
    return pool


def select_top(pool: list[NewsItem], n: int = TOP_N) -> list[NewsItem]:
    # 全局排序不设板块配额：周报回答「本周最重要的五件事」，
    # 板块均衡的诉求由周中报满足过了
    ranked = sorted(
        (i for i in pool if (i.score or 0) >= MIN_TOP_SCORE),
        key=lambda i: (-(i.score or 0), -i.published_at.timestamp()),
    )
    return ranked[:n]


def _blurb(item: NewsItem) -> str:
    if item.analysis:
        first = item.analysis.split("\n")[0]
        return first[:BLURB_CHARS] + ("…" if len(first) > BLURB_CHARS else "")
    return item.score_reason or item.summary[:BLURB_CHARS]


def render_weekly(
    top: list[NewsItem],
    pool: list[NewsItem],
    sections: WeeklySections,
    source_dates: list[str],
    usage: RunUsage,
    date_str: str,
) -> tuple[str, ReportMeta]:
    parts = [f"# AI 前沿周报 · {date_str}\n", "## 本周 Top5\n"]
    for rank, item in enumerate(top, 1):
        # 配图跟着 Top 条目走（深读时挑好存进了 JSON，这里直接复用，不重新挑图）
        imgs = "".join(f"![{img['caption']}](../../{img['path']})\n" for img in item.images)
        img_block = f"{imgs}\n" if imgs else ""
        parts.append(
            f"### {rank}. {item.title}\n\n{_blurb(item)}\n\n{img_block}"
            f"> 评分 {item.score}/10 · {item.score_reason}\n"
            f"> 来源：<{item.url}> · 发布 {item.published_at.date()}\n"
        )

    parts.append("## 本周趋势\n")
    parts.append(sections.trend or "（趋势段生成失败，本期从缺——见各期周中报）")
    parts.append("")

    parts.append("## 分板块一览\n")
    for cat in Category:
        lines = [
            f"- [{i.title}]({i.url}) — {i.score}分"
            for i in sorted(
                (i for i in pool if i.category == cat and i not in top),
                key=lambda i: -(i.score or 0),
            )[:GLANCE_CAP]
        ]
        if lines:
            parts.append(f"**{CATEGORY_NAMES[cat]}**\n\n" + "\n".join(lines) + "\n")

    parts.append("## 面试视角\n")
    parts.append(sections.interview or "（面试视角生成失败，本期从缺）")
    parts.append("")

    footer = ["## 尾注\n"]
    sources = "、".join(f"周中报 {d}" for d in source_dates)
    footer.append(f"- 数据来源：{sources}（共 {len(pool)} 条，不重新采集）")
    price_note = "分模型实测牌价" if usage.precise else "flash 未配牌价，按 pro 价上限估"
    footer.append(
        f"- 成本：入 {usage.tokens_in:,}（缓存命中 {usage.cached:,}）"
        f"/ 出 {usage.tokens_out:,} tokens ≈ ¥{usage.cost_cny:.2f}（{price_note}）"
    )
    if usage.duration_seconds is not None:
        minutes, seconds = divmod(round(usage.duration_seconds), 60)
        footer.append(f"- 耗时：{minutes} 分 {seconds} 秒")

    meta = ReportMeta(
        date=date_str,
        run_type=RunType.WEEKLY,
        description="周报 Top5：" + "；".join(i.title for i in top)[:200],
        entities=sorted({e for i in top for e in i.entities}),
        tags=[c.value for c in Category if any(i.category == c for i in top)],
        item_count=len(pool),
        tokens_used=usage.tokens_in + usage.tokens_out,
        cost_cny=round(usage.cost_cny, 4),
        duration_seconds=(
            round(usage.duration_seconds, 1) if usage.duration_seconds is not None else None
        ),
    )
    frontmatter = yaml.safe_dump(
        meta.model_dump(mode="json"), allow_unicode=True, sort_keys=False
    ).strip()
    md = f"---\n{frontmatter}\n---\n\n" + "\n".join(parts) + "\n" + "\n".join(footer) + "\n"
    return md, meta
