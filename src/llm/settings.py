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
    # 计价（元/百万 tokens）：默认按 pro 牌价对全部用量做上限估算（flash 实际更低），
    # 报告尾注会注明是上限；P5 接 LangSmith 后有精确值
    price_in: float = 12.0
    price_out: float = 24.0

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
        )
