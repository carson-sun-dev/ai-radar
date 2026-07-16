"""选题：三板块配额制（设计纪要第 8 节）——板块内竞争、板块间不竞争。

全局 top-N 会让模型发布类新闻挤掉工程实践内容，配额制保证每个板块的席位。
宁缺毋滥：板块头名分数不够深读线就整板块降级，垃圾内容不配 300 字介绍。
"""

from dataclasses import dataclass, field

from src.models import Category, NewsItem

DEEP_PER_CATEGORY = 1  # 周中报：深读 3 条（板块各 1）
MID_PER_CATEGORY = 1  # 中读 3 条（板块各 1）
MIN_DEEP_SCORE = 6  # 低于此分不值得花 pro 模型深读，落回速览
MIN_MID_SCORE = 5


@dataclass
class Selection:
    deep: list[NewsItem] = field(default_factory=list)
    mid: list[NewsItem] = field(default_factory=list)
    glance: dict[Category, list[NewsItem]] = field(default_factory=dict)  # 速览（按板块）
    unscored: list[NewsItem] = field(default_factory=list)  # 打分失败的条目，保留不丢


def select_for_midweek(scored: list[NewsItem], unscored: list[NewsItem]) -> Selection:
    selection = Selection(unscored=unscored)
    for cat in Category:
        # 同分时新的优先：资讯的时效价值随时间衰减
        pool = sorted(
            (i for i in scored if i.category == cat),
            key=lambda i: (-(i.score or 0), -i.published_at.timestamp()),
        )
        rest_start = 0
        if pool and (pool[0].score or 0) >= MIN_DEEP_SCORE:
            selection.deep.extend(pool[:DEEP_PER_CATEGORY])
            rest_start = DEEP_PER_CATEGORY
        mid_end = rest_start + MID_PER_CATEGORY
        mid_pick = [i for i in pool[rest_start:mid_end] if (i.score or 0) >= MIN_MID_SCORE]
        selection.mid.extend(mid_pick)
        selection.glance[cat] = pool[rest_start + len(mid_pick) :]
    return selection
