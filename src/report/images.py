"""深读挑图（P7，设计纪要第 6 节）：模型挑、程序下。

流程：从深读全文（Jina markdown）提候选图 → 模型挑 1-2 张关键图（只给序号）→
程序下载入 assets/ → 回填 item.images 供渲染嵌入。

纪律与 cite 一致：模型只看 caption 挑序号，不碰 URL；下载、去重、本地路径全在程序。
图片是增强不是主体——任何一步失败都静默跳过，绝不因配图废掉一条深读。
"""

import hashlib
import re
from pathlib import Path

from src.collectors import base
from src.config import RetryDefaults
from src.llm.client import ArkClient, ToolCallError
from src.models import NewsItem
from src.tools.schemas import PICK_IMAGES_TOOL

# markdown 图片语法 ![caption](url)；data:/svg 占位图和无 caption 的多为装饰，先过滤
_MD_IMG = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)")
_EXT = re.compile(r"\.(png|jpe?g|webp|gif)(\?|$)", re.I)
MAX_CANDIDATES = 12  # 给模型看的候选上限：省 token，靠前的通常是正文关键图
MAX_PICKS = 2


def _candidates(fulltext: str) -> list[tuple[str, str]]:
    """(caption, url) 候选：有 caption、看起来是位图的。去重保序。"""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for caption, url in _MD_IMG.findall(fulltext):
        cap = caption.strip()
        # 中文 caption 天然短（「架构图」3 字也有效），只滤空/单字符占位
        if len(cap) < 2 or url in seen or not _EXT.search(url):
            continue
        seen.add(url)
        out.append((cap, url))
    return out[:MAX_CANDIDATES]


def _download(url: str, caption: str, assets_dir: Path, retry: RetryDefaults) -> dict | None:
    try:
        data = base.fetch_bytes(url, retry)
    except base.FetchError:
        return None
    if len(data) < 3000:  # 太小多半是图标/间隔线，不值得配
        return None
    ext_match = _EXT.search(url)
    ext = ext_match.group(1).lower() if ext_match else "png"
    name = f"{hashlib.sha256(url.encode()).hexdigest()[:16]}.{ext}"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / name).write_bytes(data)
    return {"path": f"assets/{name}", "caption": caption}


def attach_images(
    client: ArkClient,
    item: NewsItem,
    fulltext: str,
    assets_dir: Path,
    retry: RetryDefaults,
) -> None:
    """就地给 item.images 填模型选中并成功下载的图。任何失败静默跳过。"""
    candidates = _candidates(fulltext)
    if not candidates:
        return
    listing = "\n".join(f"{i}. {cap}" for i, (cap, _) in enumerate(candidates))
    try:
        args = client.tool_call(
            model=client.settings.score_model,  # 挑图是轻判断，走便宜的 flash
            system="你在为一篇技术深读挑选配图。根据 caption 判断哪些是关键图。",
            user=f"资讯：{item.title}\n\n候选图 caption：\n{listing}",
            tool=PICK_IMAGES_TOOL,
            tags=["pick_images", item.source],
        )
    except ToolCallError:
        return  # 挑图失败不影响深读主体
    indices = args.get("indices", [])
    if not isinstance(indices, list):
        return
    picked: list[dict] = []
    for idx in indices[:MAX_PICKS]:
        if not isinstance(idx, int) or not 0 <= idx < len(candidates):
            continue  # 模型偶发越界/编造序号：跳过，不塞错图
        caption, url = candidates[idx]
        if img := _download(url, caption, assets_dir, retry):
            picked.append(img)
    item.images = picked
