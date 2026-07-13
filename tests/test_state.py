"""状态存储测试：水位线只进不退、失败不推进、去重判定——P1 验收标准的直接对应。"""

from datetime import UTC, datetime

from src.models import NewsItem
from src.state import DedupStore, StateStore


def _item(url: str, day: int) -> NewsItem:
    return NewsItem.create(
        source="test", title="t", url=url, published_at=datetime(2026, 7, day, tzinfo=UTC)
    )


class TestStateStore:
    def test_advance_moves_watermark_forward(self, tmp_path):
        store = StateStore(tmp_path / "state.json")
        assert store.watermark("openai-news") is None
        store.advance("openai-news", datetime(2026, 7, 10, tzinfo=UTC))
        assert store.watermark("openai-news") == datetime(2026, 7, 10, tzinfo=UTC)

    def test_advance_never_regresses(self, tmp_path):
        # 乱序数据（后处理的批次反而更旧）不能把窗口拉回去，否则会重复采集
        store = StateStore(tmp_path / "state.json")
        store.advance("s", datetime(2026, 7, 10, tzinfo=UTC))
        store.advance("s", datetime(2026, 7, 8, tzinfo=UTC))
        assert store.watermark("s") == datetime(2026, 7, 10, tzinfo=UTC)

    def test_failure_does_not_advance(self, tmp_path):
        # 「失败后窗口自动补齐」的核心：失败只记原因，水位线原地不动
        store = StateStore(tmp_path / "state.json")
        store.advance("s", datetime(2026, 7, 10, tzinfo=UTC))
        store.record_failure("s", "超时（已重试 3 次）")
        assert store.watermark("s") == datetime(2026, 7, 10, tzinfo=UTC)
        assert store._states["s"].last_error == "超时（已重试 3 次）"

    def test_roundtrip_persistence(self, tmp_path):
        # Actions runner 无状态：save → 重新加载必须完全还原
        path = tmp_path / "state.json"
        store = StateStore(path)
        store.advance("s", datetime(2026, 7, 10, 8, 30, tzinfo=UTC))
        store.save()
        reloaded = StateStore(path)
        assert reloaded.watermark("s") == datetime(2026, 7, 10, 8, 30, tzinfo=UTC)


class TestDedupStore:
    def test_filter_new_drops_seen_items(self, tmp_path):
        store = DedupStore(tmp_path / "seen.json")
        first = [_item("https://example.com/a", 10), _item("https://example.com/b", 11)]
        store.mark_seen(first)
        # 第二批：a 重复（带追踪参数的变体）、c 是新的
        second = [
            _item("https://example.com/a?utm_source=hf", 10),
            _item("https://example.com/c", 12),
        ]
        fresh = store.filter_new(second)
        assert [i.url for i in fresh] == ["https://example.com/c"]

    def test_old_but_unseen_item_is_kept(self, tmp_path):
        # 设计纪要第 12 节：发布时间早（如 arXiv 索引延迟）但从未见过的条目必须收
        store = DedupStore(tmp_path / "seen.json")
        store.mark_seen([_item("https://example.com/new", 12)])
        late_indexed = _item("https://example.com/old-paper", 1)  # 7 月 1 日发布，现在才见到
        assert store.filter_new([late_indexed]) == [late_indexed]

    def test_roundtrip_persistence(self, tmp_path):
        path = tmp_path / "seen.json"
        store = DedupStore(path)
        store.mark_seen([_item("https://example.com/a", 10)])
        store.save()
        assert DedupStore(path).is_seen(_item("https://example.com/a", 10).id)
