"""采集调度：按配置跑全部源，单源失败隔离（设计纪要第 11 节）。

这里不是 LangGraph 节点本体——P4 的采集节点是包住 run_all 的薄壳，
业务逻辑留在这层，保证不依赖框架也能单独运行和测试。
"""

from datetime import UTC, datetime, timedelta

from src.collectors import arxiv, github, hf_papers, rss, web
from src.collectors.base import FetchError
from src.config import CollectorKind, RadarConfig
from src.models import NewsItem
from src.state import DedupStore, StateStore

_COLLECTORS = {
    CollectorKind.RSS: rss.collect,
    CollectorKind.GITHUB: github.collect,
    CollectorKind.ARXIV: arxiv.collect,
    CollectorKind.WEB: web.collect,
    CollectorKind.HF_PAPERS: hf_papers.collect,
}


def run_all(
    config: RadarConfig, state: StateStore, dedup: DedupStore
) -> tuple[list[NewsItem], dict[str, str]]:
    """采集全部源，返回（新条目，失败源→原因）。

    失败原因 dict 直接供报告尾注使用；任何单源的异常都不允许拖垮整轮采集。
    调用方负责在整轮流程成功后统一 save 状态（Actions 里状态随报告一起 commit，
    中途崩溃则什么都不持久化，天然事务性）。
    """
    collected: list[NewsItem] = []
    failures: dict[str, str] = {}
    cutoff = datetime.now(UTC) - timedelta(days=config.max_age_days)
    for source in config.sources:
        try:
            items = _COLLECTORS[source.collector](source, config.defaults)
        except FetchError as e:
            state.record_failure(source.id, e.reason)
            failures[source.id] = e.reason
            continue
        except Exception as e:  # noqa: BLE001 —— 未预料的解析崩溃同样只废掉这一个源
            reason = f"解析失败（{type(e).__name__}）"
            state.record_failure(source.id, reason)
            failures[source.id] = reason
            continue

        # 时效闸：过期条目直接标已见后丢弃——首轮没有水位线时，
        # 这是唯一防止存量旧闻（几年前的 GitHub 仓库等）涌入打分池的机制
        stale = [i for i in items if i.published_at < cutoff]
        dedup.mark_seen(stale)
        fresh = dedup.filter_new([i for i in items if i.published_at >= cutoff])
        dedup.mark_seen(fresh)
        if items:
            # 水位线推进用「本次抓到的最新发布时间」，与是否重复无关——
            # 它标记的是「窗口推进到哪」，不是「收了多少」
            state.advance(source.id, max(i.published_at for i in items))
        collected.extend(fresh)
    return collected, failures
