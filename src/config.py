"""sources.yaml 的加载与校验。

为什么用 pydantic 严格校验而不是裸 dict：加源是常态操作（yaml 里加一段），
配置写错要在流水线启动时立刻炸出来，而不是采集到一半才发现字段缺失。
"""

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


class CollectorKind(StrEnum):
    """四种采集器 + HF Daily Papers 专用端点（JSON API，兼作打分信号源）。"""

    RSS = "rss"
    GITHUB = "github"  # org 维度的新 release / 新仓库
    ARXIV = "arxiv"
    WEB = "web"  # 无 RSS 的官网 news 页，直抓失败时 Jina Reader 兜底
    HF_PAPERS = "hf_papers"


# 每种采集器必须具备的定位字段：加载期校验，见 SourceConfig._check_target
_REQUIRED_FIELD = {
    CollectorKind.RSS: "url",
    CollectorKind.WEB: "url",
    CollectorKind.HF_PAPERS: "url",
    CollectorKind.ARXIV: "query",
    # github 采集器特殊：repo 与 github_org 二选一，单独校验
}


class SourceConfig(BaseModel):
    id: str
    org: str  # 所属机构名，报告按机构归组展示
    collector: CollectorKind
    url: str | None = None
    repo: str | None = None  # github：跟单仓库的 releases（如 langchain-ai/langgraph）
    github_org: str | None = None  # github：跟整个 org 的新 release/新仓库
    query: str | None = None  # arxiv：检索式
    # web：文章链接必须以此前缀开头（过滤导航/页脚链接）；缺省用 url 本身
    link_prefix: str | None = None
    # web：跳过直抓、直接走 Jina Reader。适用于 JS 渲染重的站点——直抓虽可能 200，
    # 但正文/标题是嵌套元素拼出的垃圾（2026-07 实测 Anthropic、Seed 均如此）
    via_jina: bool = False
    max_items: int = 50  # 单次采集上限：防止某源异常（如改版后全量重发）刷爆打分池

    @model_validator(mode="after")
    def _check_target(self) -> "SourceConfig":
        if self.collector == CollectorKind.GITHUB:
            if not (self.repo or self.github_org):
                raise ValueError(f"源 {self.id}：github 采集器需要 repo 或 github_org")
        else:
            field = _REQUIRED_FIELD[self.collector]
            if getattr(self, field) is None:
                raise ValueError(f"源 {self.id}：{self.collector.value} 采集器需要 {field} 字段")
        return self


class RetryDefaults(BaseModel):
    """重试策略（设计纪要第 11 节）：3 次指数退避，间隔 = base * factor^n。"""

    timeout_seconds: int = 30
    max_retries: int = 3
    backoff_base_seconds: int = 5
    backoff_factor: int = 3  # 5s → 15s → 45s


class RadarConfig(BaseModel):
    defaults: RetryDefaults = Field(default_factory=RetryDefaults)
    # 时效窗口：发布超过 N 天的条目采集时丢弃（标记已见，不再出现）。
    # 首轮运行/新加源时没有水位线，没有这道闸，存量旧闻（如几年前创建的
    # GitHub 仓库）会涌入打分池霸榜；30 天余量兜住 arXiv 晚收录的补漏场景
    max_age_days: int = 30
    sources: list[SourceConfig]

    @model_validator(mode="after")
    def _unique_ids(self) -> "RadarConfig":
        ids = [s.id for s in self.sources]
        if len(ids) != len(set(ids)):
            dup = {i for i in ids if ids.count(i) > 1}
            raise ValueError(f"源 id 重复：{dup}（id 是水位线和去重历史的键，必须唯一）")
        return self


def load_config(path: str | Path = "sources.yaml") -> RadarConfig:
    with open(path, encoding="utf-8") as f:
        return RadarConfig.model_validate(yaml.safe_load(f))
