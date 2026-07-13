"""GitHub 采集器：repo 模式跟 releases，org 模式跟新建仓库。

未认证的 GitHub API 限流 60 次/小时，本项目每次运行只打 6 个请求原本够用，
但 Actions runner 的出口 IP 是共享的——务必带上 GITHUB_TOKEN（workflow 里自动有）。
"""

import json
import os
from datetime import datetime

from src.collectors import base
from src.config import RetryDefaults, SourceConfig
from src.models import NewsItem

_API = "https://api.github.com"


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def collect(source: SourceConfig, retry: RetryDefaults) -> list[NewsItem]:
    if source.repo:
        return _collect_releases(source, retry)
    return _collect_org_repos(source, retry)


def _collect_releases(source: SourceConfig, retry: RetryDefaults) -> list[NewsItem]:
    per_page = min(source.max_items, 100)
    url = f"{_API}/repos/{source.repo}/releases?per_page={per_page}"
    data = json.loads(base.fetch(url, retry, _headers()))
    items = []
    for rel in data:
        if rel.get("draft") or not rel.get("published_at"):
            continue  # 草稿没有发布时间，也不该出现在资讯里
        items.append(
            NewsItem.create(
                source=source.id,
                title=f"{source.repo} {rel.get('name') or rel['tag_name']}",
                url=rel["html_url"],
                published_at=datetime.fromisoformat(rel["published_at"]),
                # release notes 就是现成的摘要，限长防止超大 changelog 撑爆打分池
                summary=(rel.get("body") or "")[:1500],
            )
        )
    return items


def _collect_org_repos(source: SourceConfig, retry: RetryDefaults) -> list[NewsItem]:
    # org 维度不逐仓库查 releases（请求数爆炸），只看新建仓库——
    # 国内厂商发新模型的习惯就是开新仓库（Qwen3-*、GLM-*），信号足够
    per_page = min(source.max_items, 100)
    url = f"{_API}/orgs/{source.github_org}/repos?sort=created&direction=desc&per_page={per_page}"
    data = json.loads(base.fetch(url, retry, _headers()))
    return [
        NewsItem.create(
            source=source.id,
            title=f"新仓库 {repo['full_name']}",
            url=repo["html_url"],
            published_at=datetime.fromisoformat(repo["created_at"]),
            summary=repo.get("description") or "",
            extra={"stars": repo.get("stargazers_count", 0)},  # 打分的外部信号之一
        )
        for repo in data
        if not repo.get("fork")  # fork 不是自家产出
    ]
