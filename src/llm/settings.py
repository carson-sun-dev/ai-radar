"""运行配置：环境变量 → 单一不可变对象。

本地开发从 .env 读（python-dotenv），GitHub Actions 从 Secrets 注入同名环境变量——
代码不感知来源差异。缺 key 在构造时立刻报错，而不是第一次调用时才发现。
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# 方舟 OpenAI 兼容 endpoint（截图确认 API 路径为 /v3/chat/completions）
DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


@dataclass(frozen=True)
class Settings:
    ark_api_key: str
    ark_base_url: str = DEFAULT_BASE_URL
    # 模型分级路由（成本工程）：打分量大而判断简单 → flash；深读/周报要质量 → pro
    # 方舟模型 ID 带版本后缀（/models 接口实测）；环境变量可覆盖，升级版本改 .env/Secrets
    score_model: str = "deepseek-v4-flash-260425"
    deepread_model: str = "deepseek-v4-pro-260425"
    # 计价（元/百万 tokens）：pro 牌价固定；flash 牌价随批价/活动变动，交给环境变量，
    # 未配置时 flash 用量按 pro 价算（保持「上限估」语义），尾注如实标注估算还是实测价
    price_in: float = 12.0
    price_out: float = 24.0
    price_in_flash: float | None = None
    price_out_flash: float | None = None
    # 方舟隐式缓存命中的输入计价（1 元/M vs 12 元/M，见 schemas.py 批量打分注释）
    price_cached: float = 1.0

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()  # 本地读 .env；CI 上文件不存在则是无害空操作
        key = os.environ.get("ARK_API_KEY", "").strip()
        if not key:
            raise RuntimeError("缺少 ARK_API_KEY：本地填 .env，GitHub Actions 配 Secrets")
        return cls(
            ark_api_key=key,
            ark_base_url=os.environ.get("ARK_BASE_URL", DEFAULT_BASE_URL),
            score_model=os.environ.get("ARK_SCORE_MODEL", cls.score_model),
            deepread_model=os.environ.get("ARK_DEEPREAD_MODEL", cls.deepread_model),
            price_in=float(os.environ.get("ARK_PRICE_IN", cls.price_in)),
            price_out=float(os.environ.get("ARK_PRICE_OUT", cls.price_out)),
            price_in_flash=_optional_float("ARK_PRICE_IN_FLASH"),
            price_out_flash=_optional_float("ARK_PRICE_OUT_FLASH"),
            price_cached=float(os.environ.get("ARK_PRICE_CACHED", cls.price_cached)),
        )


def _optional_float(name: str) -> float | None:
    # Actions 里未配置的 vars 注入为空字符串而不是缺失，float("") 会崩，统一按未配置处理
    value = os.environ.get(name, "").strip()
    return float(value) if value else None
