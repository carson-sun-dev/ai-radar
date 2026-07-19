"""采集器测试：全部走本地 fixture（P2 验收标准），不碰真实网络。

монkeypatch 的对象统一是 src.collectors.base.fetch——所有采集器都经它出网，
掐住这一个口子就能离线测试全部采集逻辑。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from src.collectors import arxiv, base, github, hf_papers, rss, runner, web
from src.collectors.base import FetchError
from src.config import RadarConfig, RetryDefaults, SourceConfig
from src.models import NewsItem
from src.state import DedupStore, StateStore

FIXTURES = Path(__file__).parent / "fixtures"
RETRY = RetryDefaults()


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _patch_fetch(monkeypatch, response: str):
    monkeypatch.setattr("src.collectors.base.fetch", lambda url, *a, **kw: response)


class TestRss:
    SOURCE = SourceConfig(id="test-rss", org="X", collector="rss", url="https://x.com/feed")

    def test_parses_entries(self, monkeypatch):
        _patch_fetch(monkeypatch, _fixture("sample_rss.xml"))
        items = rss.collect(self.SOURCE, RETRY)
        assert len(items) == 3
        assert items[0].title == "Introducing TestModel-2"
        assert items[0].published_at == datetime(2026, 7, 10, 8, 0, tzinfo=UTC)
        # summary 是打分输入：HTML 标签必须剥掉
        assert items[0].summary == "A new model with better tool use."

    def test_missing_date_falls_back_to_now(self, monkeypatch):
        _patch_fetch(monkeypatch, _fixture("sample_rss.xml"))
        no_date = rss.collect(self.SOURCE, RETRY)[2]
        assert no_date.published_at.tzinfo == UTC  # 兜底时间也必须带时区

    def test_non_feed_response_raises_parse_error(self, monkeypatch):
        # 反爬页把 feed 换成 HTML 时，要报「解析失败」而不是静默返回空
        _patch_fetch(monkeypatch, "<html>Access denied</html>")
        with pytest.raises(FetchError, match="解析失败"):
            rss.collect(self.SOURCE, RETRY)


class TestArxiv:
    SOURCE = SourceConfig(id="test-arxiv", org="arXiv", collector="arxiv", query="all:RAG")

    def test_parses_and_flattens(self, monkeypatch):
        _patch_fetch(monkeypatch, _fixture("sample_arxiv.xml"))
        items = arxiv.collect(self.SOURCE, RETRY)
        assert len(items) == 2
        # arXiv 标题/摘要常带换行缩进，必须压平
        assert items[0].title == "LightRAG-Next: Simple and Fast Retrieval"
        assert "graph-based retrieval" in items[0].summary


class TestGithub:
    def test_releases_skip_drafts(self, monkeypatch):
        _patch_fetch(monkeypatch, _fixture("sample_releases.json"))
        source = SourceConfig(
            id="t", org="LangChain", collector="github", repo="langchain-ai/langgraph"
        )
        items = github.collect(source, RETRY)
        assert len(items) == 2  # draft 被跳过，prerelease 保留（rc 也是有效信号）
        assert items[0].title == "langchain-ai/langgraph langgraph 0.6.0"
        assert items[1].title.endswith("0.6.0rc1")  # name 为 null 时退到 tag_name

    def test_org_repos_skip_forks(self, monkeypatch):
        _patch_fetch(monkeypatch, _fixture("sample_org_repos.json"))
        source = SourceConfig(id="t", org="DeepSeek", collector="github", github_org="deepseek-ai")
        items = github.collect(source, RETRY)
        assert len(items) == 1
        assert items[0].extra["stars"] == 2100  # star 数是打分的外部信号


class TestHfPapers:
    def test_parses_and_skips_malformed(self, monkeypatch):
        _patch_fetch(monkeypatch, _fixture("sample_daily_papers.json"))
        source = SourceConfig(
            id="t", org="HF", collector="hf_papers", url="https://huggingface.co/api/daily_papers"
        )
        items = hf_papers.collect(source, RETRY)
        assert len(items) == 2  # 缺 paper 字段的行跳过，不崩
        assert items[0].url == "https://huggingface.co/papers/2607.01234"
        assert items[0].extra["upvotes"] == 87  # 人工精选信号必须带给打分节点


class TestWeb:
    SOURCE = SourceConfig(
        id="t",
        org="Anthropic",
        collector="web",
        url="https://www.anthropic.com/news",
        link_prefix="https://www.anthropic.com/news/",
    )

    def test_jina_fallback_and_link_filtering(self, monkeypatch):
        # 直抓 403（Anthropic 的常态）→ 走 Jina；导航链/列表页自身/重复/短标题全部过滤
        def fake_fetch(url, *a, **kw):
            if url.startswith("https://r.jina.ai/"):
                return _fixture("sample_listing.md")
            raise FetchError("HTTP 403（疑似反爬）（已重试 3 次）")

        monkeypatch.setattr("src.collectors.base.fetch", fake_fetch)
        items = web.collect(self.SOURCE, RETRY)
        assert [i.title for i in items] == [
            "Introducing computer-use improvements",
            "Claude for Education expands",
        ]
        assert all(i.published_at.tzinfo == UTC for i in items)  # 首见时间兜底

    def test_links_summary_rescues_linkless_body(self, monkeypatch):
        # 2026-07 seed 实测：Jina 正文只剩图片卡片和纯文本标题（站点卡片改版），
        # 靠 X-With-Links-Summary 汇总节兜底提链接
        seen_headers: dict = {}
        md = (
            "![Image 1: 卡片标题](https://cdn.example.com/x.png)\n纯文本标题\n\n"
            "Links/Buttons:\n"
            "- [Real Article Title](https://www.anthropic.com/news/real-article)\n"
            "- [Careers](https://www.anthropic.com/careers)\n"
        )

        def fake_fetch(url, retry=None, headers=None, **kw):
            seen_headers.update(headers or {})
            return md

        monkeypatch.setattr("src.collectors.base.fetch", fake_fetch)
        source = SourceConfig(
            id="t",
            org="X",
            collector="web",
            url="https://www.anthropic.com/news",
            link_prefix="https://www.anthropic.com/news/",
            via_jina=True,
        )
        items = web.collect(source, RETRY)
        assert [i.title for i in items] == ["Real Article Title"]
        assert seen_headers.get("X-With-Links-Summary") == "true"

    def test_direct_html_success_skips_jina(self, monkeypatch):
        html = (
            '<a href="/news/direct-article">Direct fetch works fine</a>'
            '<a href="/careers">Careers</a>'
        )
        calls = []

        def fake_fetch(url, *a, **kw):
            calls.append(url)
            return html

        monkeypatch.setattr("src.collectors.base.fetch", fake_fetch)
        items = web.collect(self.SOURCE, RETRY)
        assert len(items) == 1 and "direct-article" in items[0].url
        assert len(calls) == 1  # 直抓成功就不该打 Jina（配额宝贵）

    def test_via_jina_goes_straight_to_jina(self, monkeypatch):
        # JS 渲染重的站点直抓拿到的是垃圾标题：via_jina 声明后一次直抓都不该发
        source = self.SOURCE.model_copy(update={"via_jina": True})
        calls: list[str] = []

        def fake_fetch(url, *a, **kw):
            calls.append(url)
            return _fixture("sample_listing.md")

        monkeypatch.setattr("src.collectors.base.fetch", fake_fetch)
        items = web.collect(source, RETRY)
        assert len(items) == 2
        assert calls == ["https://r.jina.ai/https://www.anthropic.com/news"]

    def test_no_links_extracted_raises(self, monkeypatch):
        _patch_fetch(monkeypatch, "plain text with no links at all")
        with pytest.raises(FetchError, match="未提取到文章链接"):
            web.collect(self.SOURCE, RETRY)


class TestFetchRetry:
    def test_backoff_delays_match_design(self, monkeypatch):
        # 设计定案 5/15/45：三次全超时 → 睡 5、15（最后一次失败后不再睡），报「超时」
        monkeypatch.setattr(
            httpx, "get", lambda *a, **kw: (_ for _ in ()).throw(httpx.TimeoutException("t"))
        )
        sleeps: list[int] = []
        with pytest.raises(FetchError, match="超时（已重试 3 次）"):
            base.fetch("https://x.com", RETRY, sleep=sleeps.append)
        assert sleeps == [5, 15]

    def test_recovers_within_retries(self, monkeypatch):
        # 第一次 429、第二次成功：重试的存在意义
        responses = iter([429, 200])

        def fake_get(*a, **kw):
            return httpx.Response(next(responses), content=b"ok")

        monkeypatch.setattr(httpx, "get", fake_get)
        assert base.fetch("https://x.com", RETRY, sleep=lambda s: None) == "ok"


class TestRunner:
    # 相对时间而不是写死日期：runner 有 30 天时效闸，固定日期的测试会随时间过期
    RECENT = datetime.now(UTC) - timedelta(days=1)

    def _config(self) -> RadarConfig:
        return RadarConfig.model_validate(
            {
                "sources": [
                    {"id": "good", "org": "A", "collector": "rss", "url": "https://a.com/feed"},
                    {"id": "bad", "org": "B", "collector": "rss", "url": "https://b.com/feed"},
                ]
            }
        )

    def test_single_source_failure_is_isolated(self, monkeypatch, tmp_path):
        # 设计纪要第 11 节：坏源只废自己，好源照常，失败原因供尾注
        def fake_collect(source, retry):
            if source.id == "bad":
                raise FetchError("超时（已重试 3 次）")
            return [
                NewsItem.create(
                    source=source.id, title="t", url="https://a.com/1", published_at=self.RECENT
                )
            ]

        monkeypatch.setitem(runner._COLLECTORS, "rss", fake_collect)
        state = StateStore(tmp_path / "state.json")
        dedup = DedupStore(tmp_path / "seen.json")
        collected, failures = runner.run_all(self._config(), state, dedup)

        assert [i.source for i in collected] == ["good"]
        assert failures == {"bad": "超时（已重试 3 次）"}
        assert state.watermark("good") == self.RECENT
        assert state.watermark("bad") is None  # 失败不推进：下次窗口自动补齐

    def test_second_run_yields_nothing_new(self, monkeypatch, tmp_path):
        monkeypatch.setitem(
            runner._COLLECTORS,
            "rss",
            lambda s, r: [
                NewsItem.create(
                    source=s.id, title="t", url="https://a.com/1", published_at=self.RECENT
                )
            ],
        )
        config = RadarConfig.model_validate(
            {"sources": [{"id": "good", "org": "A", "collector": "rss", "url": "https://a.com/f"}]}
        )
        state = StateStore(tmp_path / "state.json")
        dedup = DedupStore(tmp_path / "seen.json")
        first, _ = runner.run_all(config, state, dedup)
        second, _ = runner.run_all(config, state, dedup)
        assert len(first) == 1 and second == []  # 同一条目第二轮被 seen 拦下

    def test_stale_items_dropped_and_marked_seen(self, monkeypatch, tmp_path):
        # 时效闸：首轮无水位线时，存量旧闻（如 2024 年创建的仓库）不得进打分池
        stale = NewsItem.create(
            source="good",
            title="两年前的老仓库",
            url="https://a.com/old",
            published_at=datetime.now(UTC) - timedelta(days=400),
        )
        fresh = NewsItem.create(
            source="good", title="新条目", url="https://a.com/new", published_at=self.RECENT
        )
        monkeypatch.setitem(runner._COLLECTORS, "rss", lambda s, r: [stale, fresh])
        config = RadarConfig.model_validate(
            {"sources": [{"id": "good", "org": "A", "collector": "rss", "url": "https://a.com/f"}]}
        )
        dedup = DedupStore(tmp_path / "seen.json")
        collected, _ = runner.run_all(config, StateStore(tmp_path / "state.json"), dedup)
        assert collected == [fresh]
        assert dedup.is_seen(stale.id)  # 旧条目标已见：以后永不再进池
