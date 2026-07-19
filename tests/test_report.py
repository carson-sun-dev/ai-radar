"""选题与渲染测试：板块配额、宁缺毋滥、cite 完整、尾注如实。"""

from datetime import UTC, datetime

import yaml

from src.models import Category, NewsItem
from src.report.render import RunUsage, render_midweek
from src.report.select import select_for_midweek

USAGE = RunUsage(
    tokens_in=10000, tokens_out=2000, cached=3000, cost_cny=0.17,
    precise=False, duration_seconds=125.0,
)


def _item(n: int, cat: Category, score: int, day: int = 10) -> NewsItem:
    item = NewsItem.create(
        source="test",
        title=f"{cat.value} 资讯 {n}",
        url=f"https://example.com/{cat.value}/{n}",
        published_at=datetime(2026, 7, day, tzinfo=UTC),
        summary=f"摘要 {n}",
    )
    item.score = score
    item.category = cat
    item.score_reason = f"理由 {n}"
    return item


class TestSelect:
    def test_quota_per_category(self):
        # 每板块：第 1 名深读、第 2 名中读、其余速览——板块间不竞争
        scored = [
            _item(n, cat, score)
            for cat in Category
            for n, score in enumerate([9, 8, 7, 6])
        ]
        sel = select_for_midweek(scored, [])
        assert len(sel.deep) == 3 and len(sel.mid) == 3
        assert all(i.score == 9 for i in sel.deep)
        assert all(len(v) == 2 for v in sel.glance.values())

    def test_low_score_category_gets_no_deepread(self):
        # 宁缺毋滥：板块头名 5 分不够深读线（6），整板块只有速览/中读
        scored = [_item(0, Category.MODEL, 5), _item(1, Category.MODEL, 4)]
        sel = select_for_midweek(scored, [])
        assert sel.deep == []
        assert [i.score for i in sel.mid] == [5]  # 5 分够中读线
        assert [i.score for i in sel.glance[Category.MODEL]] == [4]

    def test_tie_broken_by_recency(self):
        older = _item(0, Category.PAPER, 8, day=8)
        newer = _item(1, Category.PAPER, 8, day=12)
        sel = select_for_midweek([older, newer], [])
        assert sel.deep == [newer]  # 同分新者优先：时效价值

    def test_empty_input(self):
        sel = select_for_midweek([], [])
        assert sel.deep == [] and sel.mid == [] and sel.unscored == []


class TestRender:
    def _render(self, failures=None):
        scored = [
            _item(n, cat, score)
            for cat in Category
            for n, score in enumerate([9, 7, 5])
        ]
        for i in scored:
            if i.score == 9:
                i.analysis = f"这是 {i.title} 的深读分析。" * 10
                i.entities = ["DeepSeek-V4", "KV cache 压缩"]
        sel = select_for_midweek(scored, [])
        return render_midweek(
            sel,
            failures or {},
            raw_count=50,
            usage=USAGE,
            date_str="2026-07-14",
            source_orgs=["OpenAI", "Anthropic", "字节 Seed"],
        )

    def test_frontmatter_is_valid_yaml_with_entities(self):
        md, meta = self._render()
        # frontmatter 是机器读的那一半：必须能被 YAML 解析（P6 索引依赖它）
        _, fm, _ = md.split("---\n", 2)
        parsed = yaml.safe_load(fm)
        assert parsed["run_type"] == "midweek"
        assert "DeepSeek-V4" in parsed["entities"]
        assert meta.item_count == 9

    def test_every_deep_item_has_cite(self):
        md, _ = self._render()
        # cite 纪律：深读条目的 URL 必须出现在报告里，且来自数据层
        for cat in Category:
            assert f"https://example.com/{cat.value}/0" in md

    def test_no_failures_stated_explicitly(self):
        # 「没失败」要显式说出来，读者不该猜测留白的含义
        md, _ = self._render()
        assert "本期全部源采集正常" in md

    def test_footer_lists_source_orgs(self):
        # 来源声明在尾注最前：读者先知道「这期看了谁」再看结论
        md, _ = self._render()
        assert "- 本期资源收集自：OpenAI、Anthropic、字节 Seed" in md
        assert md.index("本期资源收集自") < md.index("本期全部源采集正常")

    def test_failures_reported_honestly(self):
        md, meta = self._render(failures={"qwen-blog": "超时（已重试 3 次）"})
        assert "本期缺失源" in md and "qwen-blog：超时（已重试 3 次）" in md
        assert meta.sources_failed == ["qwen-blog：超时（已重试 3 次）"]

    def test_failed_analysis_falls_back_to_summary(self):
        scored = [_item(0, Category.MODEL, 9)]  # analysis 为空 = 深读失败
        sel = select_for_midweek(scored, [])
        empty = RunUsage(tokens_in=0, tokens_out=0, cached=0, cost_cny=0.0, precise=False)
        md, _ = render_midweek(sel, {}, 1, empty, "2026-07-14")
        assert "深读生成失败" in md and "摘要 0" in md  # 降级但如实标注

    def test_footer_usage_measured_honestly(self):
        # 尾注实测（P5）：耗时、缓存命中可见；计价方式必须标注（估算不冒充实测）
        md, meta = self._render()
        assert "耗时：2 分 5 秒" in md
        assert "缓存命中 3,000" in md
        assert "上限估" in md  # precise=False 时不许伪装成实测价
        assert meta.duration_seconds == 125.0
