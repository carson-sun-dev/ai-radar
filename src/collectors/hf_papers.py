"""HF Daily Papers 采集器：JSON API。

双重角色（设计纪要第 7 节）：论文来源 + 打分信号——上榜即人工精选背书，
upvotes 数随 extra 带给打分节点，是「防漏掉标题朴素的重磅」的外部信号之一。
"""

import json
from datetime import UTC, datetime

from src.collectors import base
from src.config import RetryDefaults, SourceConfig
from src.models import NewsItem


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def collect(source: SourceConfig, retry: RetryDefaults) -> list[NewsItem]:
    text = base.fetch(f"{source.url}?limit={source.max_items}", retry)
    try:
        rows = json.loads(text)
    except json.JSONDecodeError as e:
        raise base.FetchError("解析失败（daily_papers 返回非 JSON）") from e
    items = []
    for row in rows:
        paper = row.get("paper") or {}
        pid = paper.get("id")
        if not pid:
            continue
        items.append(
            NewsItem.create(
                source=source.id,
                title=paper.get("title", "(无标题)").strip(),
                # 统一指向 HF 论文页而不是 arXiv：同一篇论文若同时被 arxiv 源采到，
                # 语义去重在打分汇总阶段处理（设计纪要第 9 节），URL 去重管不了跨站
                url=f"https://huggingface.co/papers/{pid}",
                published_at=_parse_time(paper.get("publishedAt") or row.get("publishedAt")),
                summary=" ".join(paper.get("summary", "").split())[:1500],
                extra={"upvotes": paper.get("upvotes", 0)},
            )
        )
    return items
