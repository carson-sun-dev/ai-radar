"""规则校验测试（P5 验收：故意构造的坏输出必须被拦截）。"""

from datetime import UTC, datetime

from src.models import Category, NewsItem
from src.report.select import Selection
from src.validate.rules import MID_SENT_RANGE, check_analysis, check_quota


def _item(analysis: str = "", cat: Category = Category.MODEL) -> NewsItem:
    item = NewsItem.create(
        source="test",
        title="标题",
        url="https://example.com/x",
        published_at=datetime(2026, 7, 10, tzinfo=UTC),
        summary="摘要",
    )
    item.category = cat
    item.analysis = analysis
    return item


class TestCheckAnalysis:
    def test_valid_deep_passes(self):
        text = "这是一段合格的深读分析。" * 30  # 360 字，落在宽容带内
        assert check_analysis(_item(text), "deep") == []

    def test_deep_length_out_of_range_caught(self):
        assert check_analysis(_item("短。" * 10), "deep")  # 远低于下限
        assert check_analysis(_item("超长内容。" * 600), "deep")  # 3000 字 > 2500 护栏

    def test_thorough_deep_analysis_within_guard(self):
        # 生产实测 pro 常写 1000–1600 字：护栏放宽后不再被误清（2026-07-23 深读回归）
        assert check_analysis(_item("这是一段讲透了的深读分析。" * 120), "deep") == []

    def test_missing_analysis_caught(self):
        assert check_analysis(_item(""), "deep") == ["分析缺失"]

    def test_url_in_analysis_violates_cite_discipline(self):
        # cite 纪律：模型写出的 URL 不可信，链接只能由渲染层从数据带入
        text = "分析正文，详见 https://fake.example.com 的说明。" + "补充内容。" * 40
        problems = check_analysis(_item(text), "deep")
        assert any("URL" in p for p in problems)

    def test_mid_sentence_count_bounds(self):
        ok = "这是一句合格的中读介绍，说明了核心方法。" * 4  # 4 句
        assert check_analysis(_item(ok), "mid") == []
        assert check_analysis(_item("只有一句话没有结尾标点"), "mid")  # 0 句
        assert check_analysis(_item("很多句。" * (MID_SENT_RANGE[1] + 3)), "mid")  # 超上限


class TestCheckQuota:
    def test_within_quota_passes(self):
        sel = Selection(deep=[_item("x", Category.MODEL)], mid=[_item("x", Category.PAPER)])
        assert check_quota(sel) == []

    def test_over_quota_caught(self):
        # 配额超限只可能来自选题代码 bug，校验要能兜住这种「不可能发生」
        sel = Selection(deep=[_item("x", Category.MODEL), _item("y", Category.MODEL)])
        problems = check_quota(sel)
        assert problems and "深读配额超限" in problems[0]
