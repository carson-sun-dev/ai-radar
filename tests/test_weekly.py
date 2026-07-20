"""P6 测试：实体索引的幂等与历史查询、周报选题/渲染/两段解析、周报流水线端到端。"""

import json
from datetime import UTC, datetime

from src.llm.client import ArkClient
from src.llm.settings import Settings
from src.models import Category, NewsItem, ReportMeta, RunType
from src.pipeline.weekly import WeeklyContext, build_weekly_graph
from src.report.index import history_for, update_index
from src.report.render import RunUsage
from src.report.weekly import (
    parse_sections,
    render_weekly,
    select_top,
    week_pool,
)
from tests.test_llm import FakeOpenAI, _chat_response

USAGE = RunUsage(tokens_in=1000, tokens_out=500, cached=0, cost_cny=0.05, precise=False)


def _item(n: int, score: int, cat: Category = Category.MODEL, day: int = 15) -> NewsItem:
    item = NewsItem.create(
        source="s",
        title=f"资讯 {n}",
        url=f"https://example.com/{n}",
        published_at=datetime(2026, 7, day, tzinfo=UTC),
        summary=f"摘要 {n}",
    )
    item.score = score
    item.category = cat
    item.score_reason = f"理由 {n}"
    item.entities = [f"实体{n}"]
    item.analysis = f"这是 {n} 号条目的分析。第二句。"
    return item


def _meta(date: str, run_type: RunType = RunType.MIDWEEK) -> ReportMeta:
    return ReportMeta(date=date, run_type=run_type, description="d")


class TestIndex:
    def test_update_is_idempotent_and_lookup_respects_date(self, tmp_path):
        path = tmp_path / "index.json"
        item = _item(1, 9)
        update_index(_meta("2026-07-13"), [item], path)
        update_index(_meta("2026-07-13"), [item], path)  # 重跑/回填不重复入索
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert len(stored["实体1"]) == 1

        # 历史查询只回 before_date 之前的条目；无历史的实体不占键
        assert history_for(["实体1"], "2026-07-19", path)["实体1"][0]["title"] == "资讯 1"
        assert history_for(["实体1"], "2026-07-13", path) == {}
        assert history_for(["没见过"], "2026-08-01", path) == {}

    def test_missing_index_returns_empty(self, tmp_path):
        assert history_for(["x"], "2026-07-19", tmp_path / "no.json") == {}


class TestWeeklySelect:
    def test_pool_dedupes_across_reports(self):
        a = _item(1, 8)
        payload = {
            "deep": [a.model_dump(mode="json")],
            "mid": [],
            "glance": {"model": [_item(2, 6).model_dump(mode="json")]},
        }
        pool = week_pool([payload, payload])  # 同一份重复喂：回填重叠场景
        assert len(pool) == 2

    def test_pool_collapses_cross_source_same_paper(self):
        # 同一 arXiv 论文的 arxiv/HF 两入口 URL 不同，但 dedup_key 归一 → 只留一条
        arxiv = _item(1, 8)
        arxiv.url = "https://arxiv.org/abs/2607.14431v1"
        hf = _item(2, 8)
        hf.url = "https://huggingface.co/papers/2607.14431"
        payload = {
            "deep": [arxiv.model_dump(mode="json")],
            "mid": [hf.model_dump(mode="json")],
            "glance": {},
        }
        assert len(week_pool([payload])) == 1

    def test_top5_global_ranking_no_category_quota(self):
        # 周报是全局视角：同板块可占多席，7 分以下进不了 Top5
        pool = [
            _item(1, 9), _item(2, 9, day=16), _item(3, 8),
            _item(4, 8, Category.PAPER), _item(5, 7), _item(6, 6),
        ]
        top = select_top(pool)
        assert len(top) == 5
        assert all((i.score or 0) >= 7 for i in top)
        assert top[0].title == "资讯 2"  # 同分新者优先


class TestSections:
    def test_parse_both_markers(self):
        s = parse_sections("【趋势】本周趋势内容。\n【面试视角】三个谈点。")
        assert s.trend == "本周趋势内容。" and s.interview == "三个谈点。"

    def test_missing_marker_yields_none(self):
        s = parse_sections("模型自由发挥没带标记")
        assert s.trend is None and s.interview is None


class TestRenderWeekly:
    def test_render_contains_all_sections_and_cites(self):
        pool = [_item(n, 9 - n, day=10 + n) for n in range(6)]
        top = select_top(pool)
        s = parse_sections("【趋势】趋势正文。\n【面试视角】谈点正文。")
        md, meta = render_weekly(top, pool, s, ["2026-07-17", "2026-07-19"], USAGE, "2026-07-19")
        assert "## 本周 Top5" in md and "## 本周趋势" in md
        assert "## 分板块一览" in md and "## 面试视角" in md
        assert "https://example.com/0" in md  # cite 来自数据层
        assert "周中报 2026-07-17、周中报 2026-07-19" in md
        assert meta.run_type == RunType.WEEKLY and meta.item_count == 6

    def test_failed_generation_stated_honestly(self):
        pool = [_item(1, 9)]
        md, _ = render_weekly(
            select_top(pool), pool, parse_sections("坏输出"), ["2026-07-19"], USAGE, "2026-07-19"
        )
        assert "趋势段生成失败" in md and "面试视角生成失败" in md


def test_weekly_graph_end_to_end(tmp_path):
    # 造两份周中报 JSON + 一份含历史的索引，全假件跑通图
    month = tmp_path / "reports" / "2026-07"
    month.mkdir(parents=True)
    for date, n0 in (("2026-07-17", 0), ("2026-07-19", 10)):
        payload = {
            "meta": _meta(date).model_dump(mode="json"),
            "deep": [_item(n0 + 1, 9).model_dump(mode="json")],
            "mid": [_item(n0 + 2, 8).model_dump(mode="json")],
            "glance": {"model": [_item(n0 + 3, 6).model_dump(mode="json")]},
            "unscored": [],
        }
        (month / f"midweek-{date}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    index_path = tmp_path / "index.json"
    old = _item(1, 8)  # 与 Top5 之一同实体（实体1），date 在本周前 → 应进历史材料
    update_index(_meta("2026-07-10"), [old], index_path)

    fake = FakeOpenAI([_chat_response("【趋势】本周趋势正文。\n【面试视角】面试谈点正文。")])
    ctx = WeeklyContext(
        client=ArkClient(settings=Settings(ark_api_key="k"), client=fake),
        reports_dir=tmp_path / "reports",
        index_path=index_path,
    )
    result = build_weekly_graph(ctx).invoke({})

    md = (tmp_path / "reports").joinpath(*result["report_path"].split("/")[-2:]).read_text(
        encoding="utf-8"
    )
    assert "本周趋势正文" in md and "面试谈点正文" in md
    assert "周中报 2026-07-17" in md
    # 历史关联进了生成 prompt（演进关系的原料）
    assert "实体历史" in fake.calls[0]["messages"][1]["content"]
    # 综合生成开 thinking（一周归纳比单篇分析吃推理）
    assert fake.calls[0]["extra_body"]["thinking"]["type"] == "enabled"
