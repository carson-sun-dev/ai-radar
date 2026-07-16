"""第 1 层规则校验（零成本，设计纪要第 15 节）：坏产物不进报告。

校验对象是模型的自由文本产出——schema 校验管得住 tool call，管不住深读分析。
这里只做机器可判定的规则（字数/句数/URL 纪律/配额），忠实度是第 2 层（P8）的事。

阈值哲学：比 prompt 要求宽一档。prompt 说 300–500 字，校验放到 250–650——
卡太死会把合格产物打回重试，每次重试都是真金白银；校验要拦的是「明显坏掉」
（生成中断、跑题短文、失控长文），不是「差 20 字」。
"""

import re

from src.models import NewsItem
from src.report.select import DEEP_PER_CATEGORY, MID_PER_CATEGORY, Selection

DEEP_CHAR_RANGE = (250, 650)  # prompt 要求 300–500 字，留生成波动余量
MID_SENT_RANGE = (2, 6)  # prompt 要求 3–5 句，同上

_URL = re.compile(r"https?://")
_SENT_END = re.compile(r"[。！？!?]")


def check_analysis(item: NewsItem, kind: str) -> list[str]:
    """校验单条深读（kind='deep'）或中读（'mid'）产出，返回问题列表，空 = 合格。"""
    text = item.analysis
    if not text:
        return ["分析缺失"]
    problems: list[str] = []
    if _URL.search(text):
        # cite 纪律（设计纪要第 5 节）：链接只能由渲染层从数据带入，
        # 模型写出的 URL 无法保证真实存在——这是防幻觉的硬边界
        problems.append("正文含 URL，违反 cite 纪律")
    if kind == "deep":
        low, high = DEEP_CHAR_RANGE
        if not low <= len(text) <= high:
            problems.append(f"字数 {len(text)} 超出 {low}–{high}")
    else:
        sentences = len(_SENT_END.findall(text))
        low, high = MID_SENT_RANGE
        if not low <= sentences <= high:
            problems.append(f"句数 {sentences} 超出 {low}–{high}")
    return problems


def check_quota(selection: Selection) -> list[str]:
    """选题配额不变式。常量 import 自 select.py：调配额时校验自动跟随，
    防「选题改了、校验没改、报告被误拦」的隐性牵连。

    配额超限不是模型问题而是代码 bug（select.py 按构造保证配额），
    所以调用方对非空结果应当 fail 整轮运行而不是重试。
    """
    problems: list[str] = []
    for tier, items, limit in (
        ("深读", selection.deep, DEEP_PER_CATEGORY),
        ("中读", selection.mid, MID_PER_CATEGORY),
    ):
        counts: dict[str, int] = {}
        for item in items:
            key = item.category.value if item.category else "未归类"
            counts[key] = counts.get(key, 0) + 1
        for cat, n in counts.items():
            if n > limit:
                problems.append(f"{tier}配额超限：{cat} 板块 {n} 条 > {limit}")
    return problems
