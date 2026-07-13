"""arXiv 采集器：官方 API，返回 Atom，复用 feedparser 解析。

sortBy=submittedDate + max_items 已经把窗口压得够小，客户端不再按水位线过滤——
「没见过就收」由 DedupStore 统一判定，晚索引的论文（收录延迟数天很常见）才不会漏。
"""

from datetime import UTC, datetime
from urllib.parse import urlencode

import feedparser

from src.collectors import base
from src.config import RetryDefaults, SourceConfig
from src.models import NewsItem

_API = "https://export.arxiv.org/api/query"


def collect(source: SourceConfig, retry: RetryDefaults) -> list[NewsItem]:
    params = urlencode(
        {
            "search_query": source.query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": source.max_items,
        }
    )
    text = base.fetch(f"{_API}?{params}", retry)
    feed = feedparser.parse(text)
    if feed.bozo and not feed.entries:
        raise base.FetchError("解析失败（arXiv API 返回非 Atom）")
    items = []
    for entry in feed.entries:
        published = entry.get("published_parsed")
        items.append(
            NewsItem.create(
                source=source.id,
                # arXiv 标题常带换行缩进，压平成单行
                title=" ".join(entry.title.split()),
                url=entry.link,
                published_at=(
                    datetime(*published[:6], tzinfo=UTC) if published else datetime.now(UTC)
                ),
                summary=" ".join(entry.get("summary", "").split())[:1500],
            )
        )
    return items
