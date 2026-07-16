"""周中报流水线：采集 → 去重打分 → 选题 → 深读 → 渲染 → 落盘。

LangGraph 的角色边界（施工计划第一节纪律）：只做编排和状态传递，
每个节点是「读 state、调业务模块、写 state」的薄壳，业务逻辑全部在
collectors/llm/report 各层——不依赖框架也能单独测试和运行。

依赖注入走 PipelineContext（闭包捕获）而不是塞进 state：
state 里只放数据（可序列化），客户端/存储这类资源对象不进 state。
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from src.collectors.runner import run_all
from src.config import RadarConfig, load_config
from src.llm.client import ArkClient
from src.llm.deepread import analyze_item, fetch_fulltext
from src.llm.scoring import score_items
from src.models import NewsItem, ReportMeta
from src.report.render import render_midweek, report_payload
from src.report.select import Selection, select_for_midweek
from src.state import DedupStore, StateStore


@dataclass
class PipelineContext:
    """一次运行的全部资源与配置。测试时逐项替换假件，不动图结构。"""

    config: RadarConfig
    client: ArkClient
    state: StateStore
    dedup: DedupStore
    reports_dir: Path = field(default_factory=lambda: Path("reports"))


class MidweekState(TypedDict, total=False):
    raw_count: int
    items: list[NewsItem]
    failures: dict[str, str]
    scored: list[NewsItem]
    unscored: list[NewsItem]
    selection: Selection
    report_md: str
    report_meta: ReportMeta
    report_path: str


def build_midweek_graph(ctx: PipelineContext):
    def collect(state: MidweekState) -> MidweekState:
        items, failures = run_all(ctx.config, ctx.state, ctx.dedup)
        # raw_count 记进尾注：读者要能看出「洪峰被漏斗压到了多少」
        return {"items": items, "failures": failures, "raw_count": len(items)}

    def score(state: MidweekState) -> MidweekState:
        scored, unscored = score_items(
            ctx.client, state["items"], ctx.client.settings.score_model
        )
        return {"scored": scored, "unscored": unscored}

    def select(state: MidweekState) -> MidweekState:
        return {"selection": select_for_midweek(state["scored"], state["unscored"])}

    def deepread(state: MidweekState) -> MidweekState:
        selection = state["selection"]
        for item in selection.deep:
            # 深读配全文；抓不到就降级用摘要分析（analyze_item 内部只看有没有 fulltext）
            fulltext = fetch_fulltext(item.url, ctx.config.defaults)
            analyze_item(ctx.client, item, "deepread", fulltext)
        for item in selection.mid:
            analyze_item(ctx.client, item, "midread")
        return {}

    def render(state: MidweekState) -> MidweekState:
        client = ctx.client
        cost = (
            client.prompt_tokens * client.settings.price_in
            + client.completion_tokens * client.settings.price_out
        ) / 1e6
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        # 来源声明按机构去重（Anthropic 两路、HF 两路只报一次），失败源不算「收集自」
        failed = set(state["failures"])
        source_orgs = list(
            dict.fromkeys(s.org for s in ctx.config.sources if s.id not in failed)
        )
        md, meta = render_midweek(
            state["selection"],
            state["failures"],
            state["raw_count"],
            (client.prompt_tokens, client.completion_tokens, cost),
            date_str,
            source_orgs,
        )
        return {"report_md": md, "report_meta": meta}

    def persist(state: MidweekState) -> MidweekState:
        meta = state["report_meta"]
        month_dir = ctx.reports_dir / meta.date[:7]  # 按月份分目录（设计定案）
        month_dir.mkdir(parents=True, exist_ok=True)
        md_path = month_dir / f"midweek-{meta.date}.md"
        md_path.write_text(state["report_md"], encoding="utf-8")
        payload = report_payload(state["selection"], meta)
        (month_dir / f"midweek-{meta.date}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        # 状态最后落盘：前面任何一步崩溃都不推进水位线/去重，天然事务性
        ctx.state.save()
        ctx.dedup.save()
        return {"report_path": str(md_path)}

    graph = StateGraph(MidweekState)
    for name, fn in [
        ("collect", collect),
        ("score", score),
        ("select", select),
        ("deepread", deepread),
        ("render", render),
        ("persist", persist),
    ]:
        graph.add_node(name, fn)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "score")
    graph.add_edge("score", "select")
    graph.add_edge("select", "deepread")
    graph.add_edge("deepread", "render")
    graph.add_edge("render", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


def main() -> None:
    ctx = PipelineContext(
        config=load_config(),
        client=ArkClient(),
        state=StateStore(),
        dedup=DedupStore(),
    )
    result = build_midweek_graph(ctx).invoke({})
    print(f"报告已生成：{result['report_path']}")


if __name__ == "__main__":
    main()
