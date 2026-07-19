"""周报流水线：读本周周中报 JSON → Top5 → 历史关联 → 生成两段 → 渲染落盘。

与周中报的关键差异（设计定案）：不采集、不打分、不推水位线——周报的输入是
已经过完整质量链路的周中报 JSON，唯一的模型调用是趋势段+面试视角的综合生成。
"""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from src.llm.client import ArkClient
from src.llm.prompts import load_prompt
from src.models import NewsItem, ReportMeta
from src.report.index import INDEX_PATH, history_for
from src.report.render import RunUsage
from src.report.weekly import (
    WeeklySections,
    parse_sections,
    render_weekly,
    select_top,
    week_pool,
)

WEEK_DAYS = 7
_MIDWEEK_JSON = re.compile(r"midweek-(\d{4}-\d{2}-\d{2})\.json$")


@dataclass
class WeeklyContext:
    client: ArkClient
    reports_dir: Path = field(default_factory=lambda: Path("reports"))
    index_path: Path = field(default_factory=lambda: INDEX_PATH)
    started_at: float = field(default_factory=time.monotonic)


class WeeklyState(TypedDict, total=False):
    payloads: list[dict]
    source_dates: list[str]
    pool: list[NewsItem]
    top: list[NewsItem]
    sections: WeeklySections
    report_md: str
    report_meta: ReportMeta
    report_path: str


def _find_week_reports(reports_dir: Path, today: datetime) -> list[Path]:
    """近 7 天内的周中报 JSON，按日期升序。跨月用全目录 glob，月份分目录只是收纳。"""
    since = (today - timedelta(days=WEEK_DAYS)).strftime("%Y-%m-%d")
    found: list[tuple[str, Path]] = []
    for path in reports_dir.glob("*/midweek-*.json"):
        if (m := _MIDWEEK_JSON.search(path.name)) and since < m.group(1) <= today.strftime(
            "%Y-%m-%d"
        ):
            found.append((m.group(1), path))
    return [p for _, p in sorted(found)]


def build_weekly_graph(ctx: WeeklyContext):
    def load(state: WeeklyState) -> WeeklyState:
        paths = _find_week_reports(ctx.reports_dir, datetime.now(UTC))
        if not paths:
            # 没有周中报就没有周报：这是数据缺失不是空报告场景，直接 fail 让人看见
            raise RuntimeError("近 7 天没有周中报 JSON，周报无从生成")
        payloads = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
        return {
            "payloads": payloads,
            "source_dates": [p["meta"]["date"] for p in payloads],
        }

    def select(state: WeeklyState) -> WeeklyState:
        pool = week_pool(state["payloads"])
        return {"pool": pool, "top": select_top(pool)}

    def generate(state: WeeklyState) -> WeeklyState:
        top = state["top"]
        parts = ["## 本周 Top5 详情\n"]
        for rank, item in enumerate(top, 1):
            parts.append(
                f"{rank}. {item.title}（{item.score} 分，{item.published_at.date()}）\n"
                f"{item.analysis or item.score_reason}\n"
            )
        # 历史关联：Top5 实体在本周之前的报道摘要进上下文（演进关系的原料）
        entities = sorted({e for i in top for e in i.entities})
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        week_start = min(state["source_dates"])
        history = history_for(entities, week_start, ctx.index_path)
        if history:
            parts.append("## 实体历史（本周之前的报道）\n")
            for entity, entries in history.items():
                for e in entries:
                    parts.append(f"- {entity} @ {e['date']}：{e['title']}——{e['summary']}")
        parts.append("\n## 本周全部条目题录\n")
        parts.extend(
            f"- [{i.category.value if i.category else '未分类'}] {i.title}（{i.score} 分）"
            for i in state["pool"]
            if (i.score or 0) >= 6
        )
        # 综合生成用 pro + thinking：一周材料的归纳比单篇分析更吃推理
        text = ctx.client.chat(
            model=ctx.client.settings.deepread_model,
            system=load_prompt("weekly"),
            user="\n".join(parts),
            thinking=True,
            tags=["weekly", today],
        )
        sections = parse_sections(text)
        if sections.trend is None or sections.interview is None:
            # 标记缺失重试一次；再失败交给渲染层如实标注，不空转烧钱
            sections = parse_sections(
                ctx.client.chat(
                    model=ctx.client.settings.deepread_model,
                    system=load_prompt("weekly"),
                    user="\n".join(parts),
                    thinking=True,
                    tags=["weekly", "retry"],
                )
            )
        return {"sections": sections}

    def render(state: WeeklyState) -> WeeklyState:
        client = ctx.client
        cost, precise = client.cost_summary()
        usage = RunUsage(
            tokens_in=client.prompt_tokens,
            tokens_out=client.completion_tokens,
            cached=client.cached_tokens,
            cost_cny=cost,
            precise=precise,
            duration_seconds=time.monotonic() - ctx.started_at,
        )
        md, meta = render_weekly(
            state["top"],
            state["pool"],
            state["sections"],
            state["source_dates"],
            usage,
            datetime.now(UTC).strftime("%Y-%m-%d"),
        )
        return {"report_md": md, "report_meta": meta}

    def persist(state: WeeklyState) -> WeeklyState:
        meta = state["report_meta"]
        month_dir = ctx.reports_dir / meta.date[:7]
        month_dir.mkdir(parents=True, exist_ok=True)
        md_path = month_dir / f"weekly-{meta.date}.md"
        md_path.write_text(state["report_md"], encoding="utf-8")
        # 周报不写 JSON sidecar：它没有下游读者（周中报 JSON 才是数据本体），
        # 也不更新索引——实体都已在周中报入索，重复入索只会摊薄检索质量
        return {"report_path": str(md_path)}

    graph = StateGraph(WeeklyState)
    for name, fn in [
        ("load", load),
        ("select", select),
        ("generate", generate),
        ("render", render),
        ("persist", persist),
    ]:
        graph.add_node(name, fn)
    graph.add_edge(START, "load")
    graph.add_edge("load", "select")
    graph.add_edge("select", "generate")
    graph.add_edge("generate", "render")
    graph.add_edge("render", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


def main() -> None:
    ctx = WeeklyContext(client=ArkClient())
    run_config = {
        "run_name": f"weekly-{datetime.now(UTC):%Y-%m-%d}",
        "tags": ["weekly"],
    }
    result = build_weekly_graph(ctx).invoke({}, config=run_config)
    print(f"周报已生成：{result['report_path']}")


if __name__ == "__main__":
    main()
