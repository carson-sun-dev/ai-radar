"""数据模型测试：URL 规范化（去重键的根基）与时区纪律。"""

from datetime import UTC, datetime, timedelta, timezone

from src.models import NewsItem, make_item_id, normalize_url


class TestNormalizeUrl:
    def test_strips_tracking_params(self):
        # 同一篇文章带不同渠道的追踪参数，必须归一到同一个去重键
        assert normalize_url("https://openai.com/news/gpt?utm_source=x&utm_medium=rss") == (
            "https://openai.com/news/gpt"
        )

    def test_keeps_meaningful_params(self):
        # 非追踪参数可能区分不同内容（如分页、条目 id），不能误删
        assert "id=123" in normalize_url("https://example.com/paper?id=123&utm_source=x")

    def test_ignores_fragment_and_trailing_slash_and_case(self):
        variants = [
            "https://Example.com/blog/post/",
            "https://example.com/blog/post#section-2",
            "https://example.com/blog/post",
        ]
        assert len({normalize_url(v) for v in variants}) == 1

    def test_same_article_same_id(self):
        a = make_item_id("https://example.com/post?utm_campaign=weekly")
        b = make_item_id("https://example.com/post/")
        assert a == b


class TestNewsItem:
    def test_naive_datetime_treated_as_utc(self):
        # 部分 RSS 源的 pubDate 不带时区：按 UTC 解释而不是报错，宽容采集
        item = NewsItem.create(
            source="test",
            title="t",
            url="https://example.com/a",
            published_at=datetime(2026, 7, 10, 8, 0),
        )
        assert item.published_at.tzinfo == UTC

    def test_aware_datetime_converted_to_utc(self):
        # 北京时间 8 点 = UTC 0 点：水位线比较必须在同一时区坐标下进行
        cst = timezone(timedelta(hours=8))
        item = NewsItem.create(
            source="test",
            title="t",
            url="https://example.com/b",
            published_at=datetime(2026, 7, 10, 8, 0, tzinfo=cst),
        )
        assert item.published_at == datetime(2026, 7, 10, 0, 0, tzinfo=UTC)

    def test_id_derived_from_url(self):
        # id 不允许调用方自造：必须与 make_item_id 一致，否则去重历史失去意义
        item = NewsItem.create(
            source="test",
            title="t",
            url="https://example.com/c",
            published_at=datetime(2026, 7, 10, tzinfo=UTC),
        )
        assert item.id == make_item_id("https://example.com/c")
