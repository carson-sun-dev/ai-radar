"""两阶段漏斗第一阶段：批量打分（设计纪要第 7 节）。

批量而非逐条的原因：system prompt（rubric）约 600 token，逐条调用等于给每条
资讯重复付这笔钱；15 条一批摊薄固定开销，且方舟隐式缓存自动命中固定前缀。

失败语义：某批打分失败（tool call 烂/校验不过）时这批条目标记为「未打分」
返回而不丢弃——低分不丢弃原则的延伸，未打分条目仍进速览和实体索引。
"""

from pydantic import BaseModel, Field, ValidationError

from src.llm.client import ArkClient, ToolCallError
from src.llm.prompts import load_prompt
from src.models import Category, NewsItem
from src.tools.schemas import SUBMIT_SCORES_TOOL

BATCH_SIZE = 15


class ScoreEntry(BaseModel):
    id: str
    score: int = Field(ge=1, le=10)
    category: Category
    reason: str


class ScoreBatch(BaseModel):
    """tool call 返回值的语义校验层：schema 管形状，这里管值域（评测第 1 层）。"""

    entries: list[ScoreEntry]


def _render_item(item: NewsItem) -> str:
    # 打分输入只给标题+摘要+信号：全文属于第二阶段（深读），这里花的是小钱
    signals = []
    if upvotes := item.extra.get("upvotes"):
        signals.append(f"HF上榜 upvotes={upvotes}")
    if stars := item.extra.get("stars"):
        signals.append(f"stars={stars}")
    signal_text = "；".join(signals) or "无"
    return (
        f"id: {item.id}\n"
        f"来源: {item.source}\n"
        f"标题: {item.title}\n"
        f"摘要: {item.summary[:400] or '（无摘要）'}\n"
        f"信号: {signal_text}"
    )


def score_items(
    client: ArkClient, items: list[NewsItem], model: str
) -> tuple[list[NewsItem], list[NewsItem]]:
    """返回（已打分条目，未打分条目）。未打分 = 该批重试后仍失败，条目保留不丢。"""
    scored: list[NewsItem] = []
    unscored: list[NewsItem] = []
    system = load_prompt("score")
    for start in range(0, len(items), BATCH_SIZE):
        batch = items[start : start + BATCH_SIZE]
        user = "\n\n---\n\n".join(_render_item(i) for i in batch)
        try:
            args = client.tool_call(
                model=model, system=system, user=user, tool=SUBMIT_SCORES_TOOL
            )
            result = ScoreBatch.model_validate(args)
        except (ToolCallError, ValidationError):
            unscored.extend(batch)
            continue
        # id 对齐：模型漏答/编造 id 的条目落到未打分，绝不静默错配
        by_id = {e.id: e for e in result.entries}
        for item in batch:
            entry = by_id.get(item.id)
            if entry is None:
                unscored.append(item)
                continue
            item.score = entry.score
            item.category = entry.category
            item.score_reason = entry.reason
            scored.append(item)
    return scored, unscored
