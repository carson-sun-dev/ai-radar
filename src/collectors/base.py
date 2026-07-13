"""HTTP 获取公共层：统一重试、超时与失败原因分类（设计纪要第 11 节）。

失败原因的分类文本会一路进到报告尾注，措辞面向「两周后读报告的人」：
不同原因对应不同的修复动作（超时=网络、403=反爬要换通道、解析失败=页面改版要改代码）。
"""

import time

import httpx

from src.config import RetryDefaults

# 部分官网对无 UA 或非浏览器 UA 的请求直接 403，伪装成普通浏览器降低误伤
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}


class FetchError(Exception):
    """采集失败。reason 是分类后的中文原因，直接进报告尾注。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _classify_status(status: int) -> str:
    if status == 403:
        return "HTTP 403（疑似反爬）"
    if status == 429:
        return "HTTP 429（限流）"
    return f"HTTP {status}"


def fetch(
    url: str,
    retry: RetryDefaults | None = None,
    headers: dict[str, str] | None = None,
    sleep=time.sleep,  # 可注入：测试里不真睡 65 秒
) -> str:
    """带重试的 GET。全部尝试失败时抛 FetchError（含分类原因与重试次数）。

    403/429 也照常重试：偶发的边缘节点误判和瞬时限流占比不低，
    重试成本只有几十秒，比直接放弃一个源划算。
    """
    retry = retry or RetryDefaults()
    last_reason = "未知错误"
    for attempt in range(retry.max_retries):
        try:
            resp = httpx.get(
                url,
                timeout=retry.timeout_seconds,
                headers={**_DEFAULT_HEADERS, **(headers or {})},
                follow_redirects=True,
            )
            if resp.status_code == 200:
                return resp.text
            last_reason = _classify_status(resp.status_code)
        except httpx.TimeoutException:
            last_reason = "超时"
        except httpx.HTTPError as e:
            last_reason = f"连接失败（{type(e).__name__}）"
        if attempt < retry.max_retries - 1:
            # 指数退避 5s → 15s → 45s（sources.yaml defaults 定案）
            sleep(retry.backoff_base_seconds * retry.backoff_factor**attempt)
    raise FetchError(f"{last_reason}（已重试 {retry.max_retries} 次）")
