"""状态存储：每源水位线（data/state.json）与去重历史（data/seen.json）。

设计依据（设计纪要第 12 节）：
- 水位线 = 上次成功采集时该源最新条目的发布时间。只前进不后退——某次采集失败时
  不推进，下次窗口自动拉长补齐，失败恢复是机制免费送的。
- 时效判定是「发布时间 > 水位线 **或** 从未见过该 URL」：兜住发布晚、被索引慢的条目
  （如 arXiv 收录延迟），两个条件命中其一就收。

Actions runner 无状态，这两个文件随报告一起 commit 回仓库（bot 通道）。
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from src.models import NewsItem


def _atomic_write(path: Path, text: str) -> None:
    # 先写临时文件再原子替换：workflow 中途被杀不能留下半个 JSON，
    # 否则下次运行加载失败，整条流水线瘫痪
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class SourceState(BaseModel):
    watermark: datetime | None = None  # 该源已处理的最新条目发布时间（UTC）
    last_success_at: datetime | None = None
    last_error: str | None = None  # 失败原因分类文本，报告尾注直接引用


class StateStore:
    """水位线存储。用法：采集前读 watermark 过滤，采集成功后 advance。"""

    def __init__(self, path: str | Path = "data/state.json"):
        self.path = Path(path)
        self._states: dict[str, SourceState] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._states = {k: SourceState.model_validate(v) for k, v in raw.items()}

    def watermark(self, source_id: str) -> datetime | None:
        state = self._states.get(source_id)
        return state.watermark if state else None

    def advance(self, source_id: str, newest: datetime) -> None:
        """采集成功后推进水位线。只前进不后退：乱序数据不能把窗口拉回去。"""
        state = self._states.setdefault(source_id, SourceState())
        if state.watermark is None or newest > state.watermark:
            state.watermark = newest
        state.last_success_at = datetime.now(UTC)
        state.last_error = None

    def record_failure(self, source_id: str, reason: str) -> None:
        """失败只记原因，不碰水位线——这是「失败后窗口自动补齐」的全部实现。"""
        state = self._states.setdefault(source_id, SourceState())
        state.last_error = reason

    def save(self) -> None:
        payload = {k: v.model_dump(mode="json") for k, v in self._states.items()}
        _atomic_write(self.path, json.dumps(payload, ensure_ascii=False, indent=2))


class DedupStore:
    """去重历史：item_id → 首次见到时间（ISO 字符串）。

    语料增长很慢（每周几十条），近几年内无需清理；真需要时按首见时间裁剪即可。
    """

    def __init__(self, path: str | Path = "data/seen.json"):
        self.path = Path(path)
        self._seen: dict[str, str] = {}
        if self.path.exists():
            self._seen = json.loads(self.path.read_text(encoding="utf-8"))

    def is_seen(self, item_id: str) -> bool:
        return item_id in self._seen

    def filter_new(self, items: list[NewsItem]) -> list[NewsItem]:
        """去重判定。注意与水位线的分工（设计纪要第 12 节）：

        水位线在采集器侧决定「抓取窗口」（少抓、省请求）；到了这里，
        「从未见过该 URL」是唯一且完备的收录条件——发布时间早于水位线但没见过的
        条目（如 arXiv 索引延迟）也要收，这正是设计里「两条件命中其一」的落地。
        """
        return [item for item in items if not self.is_seen(item.id)]

    def mark_seen(self, items: list[NewsItem]) -> None:
        now = datetime.now(UTC).isoformat()
        for item in items:
            self._seen.setdefault(item.id, now)

    def save(self) -> None:
        _atomic_write(self.path, json.dumps(self._seen, ensure_ascii=False, indent=2))
