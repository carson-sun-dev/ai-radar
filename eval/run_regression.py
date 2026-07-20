"""打分 rubric 回归（评测第 3 层，设计纪要第 15 节）：改 prompt 前跑一致率。

golden.jsonl 每行一个判例（title/source/summary + 期望板块 + 期望分数区间），
多为施工期人工阅读记下的误判/漏判（问题 3、4 的判例都在里面）。本脚本用当前
rubric 对每例打分，检查板块是否命中、分数是否落在期望区间，报告一致率。

这是「prompt 工程的单元测试」，不进 pytest（要真调 LLM、有成本与随机性）：
改 score.md 前手动 `python -m eval.run_regression`，一致率下降就是改坏了信号。
默认跑 3 轮取多数——打分有随机性，单轮偶发波动不该判定 rubric 回退。
"""

import json
import sys
from collections import Counter
from pathlib import Path

from src.llm.client import ArkClient
from src.llm.scoring import score_items
from src.models import NewsItem

GOLDEN = Path(__file__).parent / "golden.jsonl"
ROUNDS = 3  # 多轮取多数，抵消单次打分随机性


def _load_cases() -> list[dict]:
    return [
        json.loads(line)
        for line in GOLDEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _case_item(case: dict, idx: int) -> NewsItem:
    # id 用行号派生，稳定且互不冲突；发布时间不影响打分，给个占位
    from datetime import UTC, datetime

    return NewsItem.create(
        source=case["source"],
        title=case["title"],
        url=f"https://golden.local/{idx}",
        published_at=datetime(2026, 7, 1, tzinfo=UTC),
        summary=case["summary"],
    )


def _majority_score(client: ArkClient, item: NewsItem) -> tuple[int | None, str | None]:
    scores: list[int] = []
    cats: list[str] = []
    for _ in range(ROUNDS):
        scored, _ = score_items(client, [item], client.settings.score_model)
        if scored and scored[0].score is not None:
            scores.append(scored[0].score)
            cats.append(scored[0].category.value if scored[0].category else "")
    if not scores:
        return None, None
    return round(sum(scores) / len(scores)), Counter(cats).most_common(1)[0][0]


def main() -> int:
    cases = _load_cases()
    client = ArkClient()
    passed = 0
    print(f"回归 {len(cases)} 个判例（每例 {ROUNDS} 轮取均值/多数）\n")
    for idx, case in enumerate(cases):
        item = _case_item(case, idx)
        score, cat = _majority_score(client, item)
        cat_ok = cat == case["expect_category"]
        score_ok = score is not None and (
            case["expect_score_min"] <= score <= case["expect_score_max"]
        )
        ok = cat_ok and score_ok
        passed += ok
        flag = "✓" if ok else "✗"
        exp = f"{case['expect_category']}/{case['expect_score_min']}-{case['expect_score_max']}"
        got = f"{cat}/{score}"
        print(f"  {flag} [{exp:>16}] 实得 {got:>12} · {case['title'][:44]}")
        if not ok:
            print(f"       └ {case['note']}")
    rate = passed / len(cases) if cases else 0
    cost, _ = client.cost_summary()
    print(f"\n一致率 {passed}/{len(cases)} = {rate:.0%} · 成本 ≈ ¥{cost:.2f}")
    # 一致率低于 80% 以非零码退出：CI 若接入可当门禁，本地当改 prompt 的红灯
    return 0 if rate >= 0.8 else 1


if __name__ == "__main__":
    sys.exit(main())
