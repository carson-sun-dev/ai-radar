"""逐源连通性探测：校准 sources.yaml 用（web 源改版、feed 失效时先跑它定位）。

与生产采集的区别：低重试快速失败（1 次、15s 超时），只求“通不通”的答案，
不求韧性。用法：uv run python scripts/probe_sources.py
"""

from src.collectors.base import FetchError
from src.collectors.runner import _COLLECTORS
from src.config import RetryDefaults, load_config

# 探测专用：快速失败
PROBE_RETRY = RetryDefaults(timeout_seconds=15, max_retries=1)


def main() -> None:
    config = load_config()
    ok, bad = 0, 0
    for source in config.sources:
        try:
            items = _COLLECTORS[source.collector](source, PROBE_RETRY)
        except FetchError as e:
            bad += 1
            print(f"✗ {source.id:24s} {e.reason}")
            continue
        except Exception as e:  # noqa: BLE001 —— 探测就是要把一切异常摆到台面上
            bad += 1
            print(f"✗ {source.id:24s} 解析崩溃：{type(e).__name__}: {e}")
            continue
        ok += 1
        newest = max(i.published_at for i in items).date() if items else "-"
        sample = items[0].title[:50] if items else ""
        print(f"✓ {source.id:24s} {len(items):3d} 条  最新 {newest}  例:{sample}")
    print(f"\n{ok} 通 / {bad} 不通（共 {ok + bad} 源）")


if __name__ == "__main__":
    main()
