"""RSS/Atom 采集器：OpenAI、DeepMind、HF blog、LangChain、Qwen blog 走这里。"""

import re
from datetime import UTC, datetime

import feedparser

from src.collectors import base
from src.config import RetryDefaults, SourceConfig
from src.models import NewsItem

_TAG = re.compile(r"<[^>]+>")


def _strip_html(text: str, limit: int = 1000) -> str:
    # summary 是第一阶段打分的输入：去标签、限长，别把 HTML 噪音喂给模型浪费 token
    return _TAG.sub("", text).strip()[:limit]


def _entry_time(entry) -> datetime:
    # published 缺失时退到 updated；都没有则用当前时间 + 去重历史兜底（设计纪要第 12 节）
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime(*parsed[:6], tzinfo=UTC)
    return datetime.now(UTC)


def collect(source: SourceConfig, retry: RetryDefaults) -> list[NewsItem]:
    text = base.fetch(source.url, retry)
    feed = feedparser.parse(text)
    # feedparser 对非 feed 输入很宽容（纯 HTML 也不设 bozo），可靠判据是 version：
    # 合法 feed 哪怕零条目也有版本号，被反爬页替换的响应没有
    if not feed.entries and not feed.version:
        raise base.FetchError("解析失败（响应不是合法 feed）")
    return [
        NewsItem.create(
            source=source.id,
            title=entry.get("title", "(无标题)").strip(),
            url=entry.link,
            published_at=_entry_time(entry),
            summary=_strip_html(entry.get("summary", "")),
        )
        for entry in feed.entries[: source.max_items]
        if entry.get("link")
    ]
