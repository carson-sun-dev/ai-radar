"""流水线端到端测试：全假件跑通 LangGraph 图（P4 的离线验收）。

假件边界：采集（monkeypatch run_all）与 LLM（假 OpenAI 注入 ArkClient），
选题/渲染/落盘走真实代码——测的是「图的接线」而不是各节点内部（那些有各自的单测）。
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from src.config import RadarConfig
from src.llm.client import ArkClient
from src.llm.settings import Settings
from src.models import NewsItem
from src.pipeline.midweek import PipelineContext, build_midweek_graph
from src.state import DedupStore, StateStore
from tests.test_llm import FakeOpenAI, _chat_response, _response

CONFIG = RadarConfig.model_validate(
    {"sources": [{"id": "s1", "org": "X", "collector": "rss", "url": "https://x.com/f"}]}
)


def _fake_items() -> list[NewsItem]:
    # 相对时间：runner 层有 30 天时效闸，固定日期会让测试随时间过期
    recent = datetime.now(UTC) - timedelta(days=1)
    return [
        NewsItem.create(
            source="s1",
            title=f"资讯 {n}",
            url=f"https://example.com/{n}",
            published_at=recent,
            summary=f"摘要 {n}",
        )
        for n in range(4)
    ]


def _score_response(items):
    return json.dumps(
        {
            "entries": [
                {
                    "id": i.id,
                    "score": 9 - n,
                    "category": "engineering",
                    "reason": f"理由 {n}",
                }
                for n, i in enumerate(items)
            ]
        }
    )


def test_midweek_graph_end_to_end(monkeypatch, tmp_path):
    items = _fake_items()

    def fake_run_all(config, state, dedup):
        # 假件也要履行 run_all 的契约（推进水位线、记 seen）：
        # 这样末尾才能验证 persist 节点确实把内存状态写到了盘上
        dedup.mark_seen(items)
        state.advance("s1", max(i.published_at for i in items))
        return items, {"bad": "超时"}

    monkeypatch.setattr("src.pipeline.midweek.run_all", fake_run_all)
    # 深读的全文抓取不出网
    monkeypatch.setattr(
        "src.pipeline.midweek.fetch_fulltext", lambda url, retry: "全文内容" * 100
    )
    entities = json.dumps({"entities": ["LangGraph"]})
    # 假产出要过 P5 规则校验（深读 250–650 字、中读 2–6 句），否则触发重试耗尽假响应
    fake_llm = FakeOpenAI(
        [
            _response(_score_response(items)),  # score 节点：一批 4 条
            _chat_response("这是深读分析。" * 50),  # deep：分析走普通 chat
            _response(entities),  # deep：实体走小 tool call
            _chat_response("这是一段合格的中读分析，讲清了方法与变化。" * 3),  # mid：同上
            _response(entities),
        ]
    )
    ctx = PipelineContext(
        config=CONFIG,
        client=ArkClient(settings=Settings(ark_api_key="k"), client=fake_llm),
        state=StateStore(tmp_path / "state.json"),
        dedup=DedupStore(tmp_path / "seen.json"),
        reports_dir=tmp_path / "reports",
    )
    result = build_midweek_graph(ctx).invoke({})

    # 报告落盘：md + json 成对出现，按月分目录
    md_path = Path(result["report_path"])
    assert md_path.exists() and md_path.parent.name == md_path.stem.split("-", 1)[1][:7]
    payload = json.loads(md_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert len(payload["deep"]) == 1 and len(payload["mid"]) == 1
    assert len(payload["glance"]["engineering"]) == 2

    md = md_path.read_text(encoding="utf-8")
    assert "这是深读分析。" in md  # 深读产出进了报告
    assert "https://example.com/0" in md  # cite 来自数据层
    assert "bad：超时" in md  # 缺失源如实进尾注
    front = yaml.safe_load(md.split("---\n", 2)[1])
    assert front["entities"] == ["LangGraph"]

    # 状态落盘：水位线推进 + 去重历史记录（下次运行不重复）
    assert StateStore(tmp_path / "state.json").watermark("s1") is not None
    assert DedupStore(tmp_path / "seen.json").is_seen(items[0].id)

    # 尾注实测（P5）：耗时与缓存命中出现在报告里
    assert "耗时：" in md and "缓存命中" in md


def test_validate_node_intercepts_bad_output_and_retries(monkeypatch, tmp_path):
    """P5 验收：故意构造的坏输出（中读 10 句超限）被校验拦截，重试后合格产物进报告。"""
    items = _fake_items()

    def fake_run_all(config, state, dedup):
        dedup.mark_seen(items)
        state.advance("s1", max(i.published_at for i in items))
        return items, {}

    monkeypatch.setattr("src.pipeline.midweek.run_all", fake_run_all)
    monkeypatch.setattr(
        "src.pipeline.midweek.fetch_fulltext", lambda url, retry: "全文内容" * 100
    )
    entities = json.dumps({"entities": ["LangGraph"]})
    bad_mid = "句子很多呀。" * 10  # 60 字过 analyze_item 的门槛，但 10 句超中读上限
    good_mid = "这是重试后合格的中读分析，讲清了方法与变化。" * 3
    fake_llm = FakeOpenAI(
        [
            _response(_score_response(items)),
            _chat_response("这是深读分析。" * 50),  # deep 一次合格
            _response(entities),
            _chat_response(bad_mid),  # mid 首次产出坏文本
            _response(entities),
            _chat_response(good_mid),  # validate 节点重试
            _response(entities),
        ]
    )
    ctx = PipelineContext(
        config=CONFIG,
        client=ArkClient(settings=Settings(ark_api_key="k"), client=fake_llm),
        state=StateStore(tmp_path / "state.json"),
        dedup=DedupStore(tmp_path / "seen.json"),
        reports_dir=tmp_path / "reports",
    )
    result = build_midweek_graph(ctx).invoke({})

    md = Path(result["report_path"]).read_text(encoding="utf-8")
    assert "这是重试后合格的中读分析" in md  # 重试产物顶替坏产物
    assert bad_mid not in md  # 坏产物没有溜进报告
    assert len(fake_llm.calls) == 7  # 恰好多出一轮 mid 重试（分析+实体）
