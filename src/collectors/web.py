"""网页采集器：无 RSS 的官网 news/blog 列表页（Anthropic、DeepSeek、GLM、Seed）。

现实情况：这些列表页大多是 JS 渲染（尤其 Anthropic），直抓 HTML 常常提不到内容，
所以策略是「直抓提链接，提不到或被拒就走 Jina Reader」——Jina 渲染后返回 markdown，
链接以 [标题](URL) 形式出现，反而更好解析。

列表页通常拿不到发布日期：published_at 记首次见到时间，防重复靠 seen 历史
（设计纪要第 12 节的两条件设计正是为这种源准备的）。
"""

import re
from datetime import UTC, datetime
from urllib.parse import urljoin

from src.collectors import base
from src.config import RetryDefaults, SourceConfig
from src.models import NewsItem, normalize_url

_JINA = "https://r.jina.ai/"
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HTML_LINK = re.compile(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', re.S)
_TAG = re.compile(r"<[^>]+>")


def _links_from_html(html: str, base_url: str) -> list[tuple[str, str]]:
    return [
        (_TAG.sub("", text).strip(), urljoin(base_url, href))
        for href, text in _HTML_LINK.findall(html)
    ]


def _links_from_markdown(md: str) -> list[tuple[str, str]]:
    return [(title.strip(), url) for title, url in _MD_LINK.findall(md)]


def collect(source: SourceConfig, retry: RetryDefaults) -> list[NewsItem]:
    prefix = normalize_url(source.link_prefix or source.url)
    listing = normalize_url(source.url)

    links: list[tuple[str, str]] = []
    if not source.via_jina:
        try:
            links = _links_from_html(base.fetch(source.url, retry), source.url)
        except base.FetchError:
            pass  # 直抓失败不是终点，下面走 Jina 兜底
        links = [(t, u) for t, u in links if normalize_url(u).startswith(prefix)]
    if not links:
        md = base.fetch(f"{_JINA}{source.url}", retry)
        links = [(t, u) for t, u in _links_from_markdown(md) if normalize_url(u).startswith(prefix)]

    now = datetime.now(UTC)
    items: list[NewsItem] = []
    picked: set[str] = set()
    for title, url in links:
        norm = normalize_url(url)
        if norm == listing or norm in picked:
            continue  # 列表页自身、同页重复链接
        if len(title) < 4:
            continue  # 「更多」「Read」这类导航短链，不是文章
        picked.add(norm)
        items.append(
            NewsItem.create(source=source.id, title=title[:200], url=url, published_at=now)
        )
    if not items:
        # 两条通道都拿到了响应却提不出文章链接 = 页面结构变了，要改 link_prefix 或解析规则
        raise base.FetchError("解析失败（列表页未提取到文章链接）")
    return items[: source.max_items]
