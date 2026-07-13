"""LLM 层测试：客户端 tool call 往返/重试、prompt 加载、批量打分（P3 验收）。

全部离线：通过 ArkClient 的 client 注入口塞假 OpenAI 对象，不碰网络、不需要 key。
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.llm.client import ArkClient, ToolCallError
from src.llm.prompts import load_prompt
from src.llm.scoring import score_items
from src.llm.settings import Settings
from src.models import Category, NewsItem
from src.tools.schemas import SUBMIT_SCORES_TOOL

SETTINGS = Settings(ark_api_key="test-key")


def _response(arguments: str | None, usage=(100, 20)):
    """构造 OpenAI SDK 响应的鸭子类型替身。arguments=None 模拟模型没返回 tool call。"""
    tool_calls = None
    if arguments is not None:
        tool_calls = [SimpleNamespace(function=SimpleNamespace(arguments=arguments))]
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=tool_calls))],
        usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]),
    )


class FakeOpenAI:
    def __init__(self, responses):
        self.calls: list[dict] = []
        self._responses = iter(responses)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return next(self._responses)


def _client(responses) -> tuple[ArkClient, FakeOpenAI]:
    fake = FakeOpenAI(responses)
    return ArkClient(settings=SETTINGS, client=fake), fake


def _item(n: int) -> NewsItem:
    return NewsItem.create(
        source="test",
        title=f"资讯 {n}",
        url=f"https://example.com/{n}",
        published_at=datetime(2026, 7, 10, tzinfo=UTC),
        summary=f"摘要 {n}",
    )


class TestArkClient:
    def test_returns_parsed_arguments(self):
        client, fake = _client([_response('{"entries": []}')])
        result = client.tool_call(
            model="m", system="s", user="u", tool=SUBMIT_SCORES_TOOL
        )
        assert result == {"entries": []}
        # 强制 tool_choice：结构化输出的唯一保证（方舟无裸 JSON mode）
        assert fake.calls[0]["tool_choice"]["function"]["name"] == "submit_scores"
        # thinking 默认关闭：打分不该为思考 token 付费
        assert fake.calls[0]["extra_body"]["thinking"]["type"] == "disabled"

    def test_retries_on_missing_tool_call_then_succeeds(self):
        client, fake = _client([_response(None), _response('{"entries": []}')])
        assert client.tool_call(model="m", system="s", user="u", tool=SUBMIT_SCORES_TOOL) == {
            "entries": []
        }
        assert len(fake.calls) == 2

    def test_raises_after_exhausting_attempts(self):
        client, _ = _client([_response(None), _response("not json{")])
        with pytest.raises(ToolCallError, match="已尝试 2 次"):
            client.tool_call(model="m", system="s", user="u", tool=SUBMIT_SCORES_TOOL)

    def test_accumulates_usage_across_calls(self):
        # 用量跨调用累计：P5 的成本尾注依赖这个数
        client, _ = _client(
            [
                _response('{"entries": []}', usage=(100, 20)),
                _response('{"entries": []}', usage=(50, 10)),
            ]
        )
        for _ in range(2):
            client.tool_call(model="m", system="s", user="u", tool=SUBMIT_SCORES_TOOL)
        assert (client.prompt_tokens, client.completion_tokens) == (150, 30)


class TestPrompts:
    def test_score_prompt_loads_and_strips_design_comments(self):
        text = load_prompt("score")
        assert "铁律" in text and "评分维度" in text
        # 设计意图注释是给维护者的，不该花 token 也不该影响模型
        assert "设计意图" not in text and "<!--" not in text


class TestScoring:
    def _valid_arguments(self, items) -> str:
        return json.dumps(
            {
                "entries": [
                    {
                        "id": i.id,
                        "score": 7,
                        "category": "paper",
                        "reason": "有方法有代码",
                    }
                    for i in items
                ]
            }
        )

    def test_ten_items_all_scored_and_validated(self):
        # P3 验收标准：10 条 fixture 资讯打分，输出全部通过 schema 校验
        items = [_item(n) for n in range(10)]
        client, fake = _client([_response(self._valid_arguments(items))])
        scored, unscored = score_items(client, items, model="flash")
        assert len(scored) == 10 and unscored == []
        assert all(i.score == 7 and i.category == Category.PAPER for i in scored)
        assert len(fake.calls) == 1  # 10 条 < 批大小 15，一次调用完成

    def test_batching_splits_large_input(self):
        items = [_item(n) for n in range(20)]  # 15 + 5 → 两批
        client, fake = _client(
            [
                _response(self._valid_arguments(items[:15])),
                _response(self._valid_arguments(items[15:])),
            ]
        )
        scored, _ = score_items(client, items, model="flash")
        assert len(scored) == 20 and len(fake.calls) == 2

    def test_missing_id_falls_to_unscored(self):
        # 模型漏答一条：那条进未打分（保留不丢），其余正常，绝不静默错配
        items = [_item(n) for n in range(3)]
        client, _ = _client([_response(self._valid_arguments(items[:2]))])
        scored, unscored = score_items(client, items, model="flash")
        assert len(scored) == 2 and unscored == [items[2]]

    def test_out_of_range_score_fails_batch_softly(self):
        # score=99 过得了 JSON 解析、过不了语义校验（评测第 1 层）：整批标未打分
        items = [_item(0)]
        bad = json.dumps(
            {"entries": [{"id": items[0].id, "score": 99, "category": "paper", "reason": "x"}]}
        )
        client, _ = _client([_response(bad)])
        scored, unscored = score_items(client, items, model="flash")
        assert scored == [] and unscored == items
