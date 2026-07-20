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
# Jina 把卡片标题尾部的发布日期直接粘在标题后（"…for AI agentsSep 29, 2025"）——
# 这既是脏标题的来源，也是唯一能拿到的真实发布日期，一并提取
_TRAILING_DATE = re.compile(
    r"(?P<title>.+?)\s*"
    r"(?P<date>(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\.?\s+\d{1,2},\s+\d{4})$"
)
# 图片 alt（"![Image 1: 干净标题](...)"）是 Featured 卡片唯一的干净标题来源：
# 汇总节里 Featured 条目会把 "Featured"+标题+整段摘要粘成一行，靠 alt 截回标题
_IMG_ALT = re.compile(r"!\[Image \d+:\s*(.+?)\]")


def _image_alts(md: str) -> list[str]:
    return [a.strip() for a in _IMG_ALT.findall(md) if len(a.strip()) >= 4]


def _clean_title_and_date(raw: str, alts: list[str]) -> tuple[str, datetime | None]:
    """把 Jina 卡片文本拆成（干净标题，发布日期或 None）。

    治三种脏：尾部日期（顺带取真实日期）、"Featured" 前缀、摘要被粘在标题后
    （用图片 alt 作已知干净标题把后缀截掉）。都治不了时原样返回，交给下游截断。
    """
    text = raw.strip()
    published: datetime | None = None
    if m := _TRAILING_DATE.match(text):
        text = m.group("title").strip()
        try:
            published = datetime.strptime(
                m.group("date").replace(".", ""), "%b %d, %Y"
            ).replace(tzinfo=UTC)
        except ValueError:
            published = None  # 月份缩写异常等：宁可退回首见时间也不塞脏日期
    text = text.removeprefix("Featured").strip()
    # 摘要粘连：标题以某个已知 alt 开头且更长 → 截到 alt（alt 长者优先，避免截过头）
    for alt in sorted(alts, key=len, reverse=True):
        if text.startswith(alt) and len(text) > len(alt):
            return alt, published
    return text, published


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
    alts: list[str] = []
    if not source.via_jina:
        try:
            links = _links_from_html(base.fetch(source.url, retry), source.url)
        except base.FetchError:
            pass  # 直抓失败不是终点，下面走 Jina 兜底
        links = [(t, u) for t, u in links if normalize_url(u).startswith(prefix)]
    if not links:
        # 链接汇总头：Jina 正文 markdown 不保证含卡片链接（2026-07 seed 实测：站点卡片
        # 改版后正文只剩图片和纯文本标题），汇总节稳定列出页内全部 <a>，是更可靠的提取源
        md = base.fetch(
            f"{_JINA}{source.url}", retry, headers={"X-With-Links-Summary": "true"}
        )
        alts = _image_alts(md)
        links = [(t, u) for t, u in _links_from_markdown(md) if normalize_url(u).startswith(prefix)]

    now = datetime.now(UTC)
    items: list[NewsItem] = []
    picked: set[str] = set()
    for raw_title, url in links:
        norm = normalize_url(url)
        if norm == listing or norm in picked:
            continue  # 列表页自身、同页重复链接
        title, published = _clean_title_and_date(raw_title, alts)
        if len(title) < 4:
            continue  # 「更多」「Read」这类导航短链，不是文章
        picked.add(norm)
        # 拿到真实日期就用它（存量旧文靠它被 runner 的时效闸挡住），否则退回首见时间
        items.append(
            NewsItem.create(
                source=source.id, title=title[:200], url=url, published_at=published or now
            )
        )
    if not items:
        # 两条通道都拿到了响应却提不出文章链接 = 页面结构变了，要改 link_prefix 或解析规则
        raise base.FetchError("解析失败（列表页未提取到文章链接）")
    return items[: source.max_items]
