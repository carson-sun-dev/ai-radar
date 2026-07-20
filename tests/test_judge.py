"""忠实度 judge 测试（P8，评测第 2 层）：judge 判定、重生成、低置信标注、渲染。"""

from datetime import UTC, datetime

from src.llm.client import ArkClient
from src.llm.settings import Settings
from src.models import Category, NewsItem
from src.report.render import RunUsage, render_midweek
from src.report.select import select_for_midweek
from src.validate.judge import FAITHFULNESS_MIN, judge_faithfulness
from tests.test_llm import FakeOpenAI, _response

USAGE = RunUsage(tokens_in=0, tokens_out=0, cached=0, cost_cny=0.0, precise=False)


def _client(responses):
    return ArkClient(settings=Settings(ark_api_key="k"), client=FakeOpenAI(responses))


class TestJudge:
    def test_faithful_analysis_passes(self):
        client = _client([_response('{"score": 5, "unsupported": []}')])
        v = judge_faithfulness(client, "分析正文", "原文正文")
        assert v.score == 5 and v.passed and v.unsupported == []

    def test_low_score_fails_with_reasons(self):
        client = _client([_response('{"score": 2, "unsupported": ["编造了 40% 的加速数字"]}')])
        v = judge_faithfulness(client, "分析", "原文")
        assert not v.passed and v.score < FAITHFULNESS_MIN
        assert "40%" in v.unsupported[0]

    def test_judge_failure_does_not_fail_analysis(self):
        # judge 本身调用失败（tool call 烂）：不冤枉分析，按放行处理（宁放过不错杀）
        client = _client([_response(None), _response(None), _response(None), _response(None)])
        v = judge_faithfulness(client, "分析", "原文")
        assert not v.judged and v.passed

    def test_out_of_range_score_rejected_as_judge_failure(self):
        # score=9 超 1-5 值域：pydantic 拦下 → judged=False → 放行（不错杀）
        client = _client([_response('{"score": 9, "unsupported": []}')])
        v = judge_faithfulness(client, "分析", "原文")
        assert not v.judged and v.passed


class TestLowConfidenceRender:
    def _item(self, low: bool) -> NewsItem:
        item = NewsItem.create(
            source="s", title="深读条目", url="https://x.com/a",
            published_at=datetime(2026, 7, 18, tzinfo=UTC),
        )
        item.score, item.category = 9, Category.MODEL
        item.score_reason, item.analysis = "理由", "这是一段深读分析。" * 20
        item.low_confidence = low
        return item

    def test_low_confidence_shows_caveat(self):
        sel = select_for_midweek([self._item(low=True)], [])
        md, _ = render_midweek(sel, {}, 1, USAGE, "2026-07-19")
        assert "⚠ 忠实度存疑" in md

    def test_faithful_item_no_caveat(self):
        sel = select_for_midweek([self._item(low=False)], [])
        md, _ = render_midweek(sel, {}, 1, USAGE, "2026-07-19")
        assert "忠实度存疑" not in md
