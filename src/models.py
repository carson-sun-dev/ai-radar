"""核心数据模型：NewsItem 贯穿整条流水线，ReportMeta 是报告的机器可读层。

设计约束（设计纪要第 5、6 节）：
- source_url 从采集起随数据走，模型只消费不生成——防引用幻觉的机制保证
- published_at 一律 UTC：水位线比较、实体索引时间线都依赖它，时区混乱会让历史关联失真
"""

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator


def normalize_url(url: str) -> str:
    """URL 规范化：同一篇文章的不同链接形态必须归一，否则去重失效。

    处理三类常见噪音：追踪参数（utm_* 等）、fragment、末尾斜杠。
    """
    parts = urlsplit(url.strip())
    # 追踪参数不影响指向的内容，但会让同一 URL 产生无数变体
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.startswith(("utm_", "ref_"))]
    return urlunsplit(
        (
            parts.scheme.lower() or "https",
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            urlencode(query),
            "",  # fragment 只影响页内定位，丢弃
        )
    )


def make_item_id(url: str) -> str:
    """去重键：规范化 URL 的 sha256 前 16 位。

    用哈希而不是原始 URL 做键，是为了 seen.json 里键长可控且无转义问题。
    """
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()[:16]


class Category(StrEnum):
    """三板块（设计纪要第 8 节）：板块内竞争、板块间不竞争，保工程内容席位。"""

    MODEL = "model"  # 模型动态：发布、能力更新、价格
    ENGINEERING = "engineering"  # 工程实践：官方工程博客、框架最佳实践
    PAPER = "paper"  # 论文/新技术


class RunType(StrEnum):
    MIDWEEK = "midweek"  # 周二/周五周中报
    WEEKLY = "weekly"  # 周日周报（只读周中报 JSON，不重新采集）


class NewsItem(BaseModel):
    """一条资讯的全生命周期载体：采集时创建，打分/深读阶段逐步填充可选字段。"""

    id: str
    source: str  # 对应 sources.yaml 里的源 id
    title: str
    url: str
    published_at: datetime  # 一律 UTC；水位线比较的依据是它，不是抓取时间
    summary: str = ""  # 原文摘要/开头段：两阶段漏斗第一阶段的打分输入
    # ---- 以下由流水线后续节点填充 ----
    category: Category | None = None
    score: int | None = None  # 1–10，rubric 见 prompts/
    score_reason: str = ""
    entities: list[str] = Field(default_factory=list)  # 实体索引/历史关联的原料
    analysis: str = ""  # 深读/中读产出的中文技术介绍（深读 300–500 字，中读 3–5 句）
    extra: dict = Field(default_factory=dict)  # 源特定信号（如 HF papers 点赞数、star 增速）

    @field_validator("published_at")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        # 宽容处理无时区的输入（部分 RSS 源不带 tz），按 UTC 解释；有时区的统一转 UTC
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v.astimezone(UTC)

    @classmethod
    def create(
        cls, *, source: str, title: str, url: str, published_at: datetime, **kw
    ) -> "NewsItem":
        """统一入口：id 必须由 URL 派生，不允许调用方自造，否则去重键失去一致性。"""
        return cls(
            id=make_item_id(url),
            source=source,
            title=title,
            url=url,
            published_at=published_at,
            **kw,
        )


class ReportMeta(BaseModel):
    """报告 frontmatter（机器可读层）：进 index.json 供历史关联检索。

    「一个文件两个受众」：本模型渲染成 YAML frontmatter，正文 markdown 给人读。
    """

    date: str  # YYYY-MM-DD
    run_type: RunType
    description: str  # 一句话概括本期，历史关联检索时的第一判断依据
    entities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    item_count: int = 0
    sources_failed: list[str] = Field(default_factory=list)  # 尾注「本期 X 源缺失」的数据来源
    # 成本实测（设计纪要第 15 节）：从 LangSmith trace 汇总，印进尾注
    tokens_used: int = 0
    cost_cny: float = 0.0
