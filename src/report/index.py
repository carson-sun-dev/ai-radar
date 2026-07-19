"""实体索引：深读/中读条目的实体 → 历次报道（设计定案：不部署向量库）。

index.json 是历史关联的检索层——周报写趋势时按实体名精确匹配旧条目，把旧摘要
送进上下文让模型写「演进关系」（Seed2.1 相比上期 Seed2.0 变了什么）。只索引
深读/中读条目：实体抽取只发生在这两档，速览条目没有实体也没有分析可供回看。

JSON 为本体、精确匹配为检索方式，是设计纪要的刻意取舍：实体名在抽取时已被
规范化（模型名带版本、技术名用通用写法），量级在千条以内，未来按需迁 sqlite-vec。
"""

import json
from pathlib import Path

from src.models import NewsItem, ReportMeta

INDEX_PATH = Path("data/index.json")
SUMMARY_CHARS = 200  # 入索摘要长度：够模型回忆起「上次说了什么」，不够撑爆上下文


def update_index(
    meta: ReportMeta, items: list[NewsItem], path: Path = INDEX_PATH
) -> None:
    """把一份报告的深读/中读条目按实体并入索引。按条目 id 幂等：重跑/回填不重复。"""
    index: dict[str, list[dict]] = (
        json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    )
    for item in items:
        for entity in item.entities:
            entries = index.setdefault(entity, [])
            if any(e["id"] == item.id for e in entries):
                continue
            entries.append(
                {
                    "id": item.id,
                    "date": meta.date,
                    "run_type": meta.run_type.value,
                    "title": item.title,
                    "url": item.url,
                    "score": item.score,
                    "summary": (item.analysis or item.summary)[:SUMMARY_CHARS],
                }
            )
            entries.sort(key=lambda e: e["date"])
    # 原子写（与 state.py 同一纪律）：中途被杀不能留半个 JSON 瘫痪下次运行
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def history_for(
    entities: list[str],
    before_date: str,
    path: Path = INDEX_PATH,
    per_entity: int = 3,
) -> dict[str, list[dict]]:
    """查实体在 before_date（不含）之前的历史条目，每实体取最近 per_entity 条。

    返回里只有真有历史的实体——空历史不该占用周报 prompt 的一个字。
    """
    if not path.exists():
        return {}
    index = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, list[dict]] = {}
    for entity in entities:
        past = [e for e in index.get(entity, []) if e["date"] < before_date]
        if past:
            result[entity] = past[-per_entity:]
    return result
