"""方舟 DeepSeek 客户端封装：强制 tool call 的一次往返。

三个存在理由：
- 结构化输出的唯一通道是 tool call——方舟的 DeepSeek-V4 不支持裸 JSON mode
  （模型详情页「结构化输出」未勾选），tool_choice 强制指定函数名保证必返回结构
- thinking 显式控制：思考 token 按输出价（24 元/M）计费，打分任务默认关闭，
  深读按需开启；不显式传参就把成本交给了平台默认值
- token 用量分模型累计（含缓存命中）：flash/pro 牌价不同，混在一起就算不出
  精确成本；尾注的成本实测（P5）依赖这份账本

可观测（P5）：LANGSMITH_TRACING=true 且有 LANGSMITH_API_KEY 时用 wrap_openai
包住真实客户端，每次 LLM 调用成为 LangSmith trace 里的 span；注入的假客户端
（测试）不包装——观测是生产设施，不该改变测试的行为边界。
"""

import json
import os
from dataclasses import dataclass

from openai import OpenAI

from src.llm.settings import Settings


class ToolCallError(Exception):
    """模型未按要求返回 tool call，或 arguments 不是合法 JSON（重试耗尽后抛出）。"""


@dataclass
class ModelUsage:
    """单个模型的用量账本。cached 是 prompt 的子集（方舟隐式缓存命中部分）。"""

    prompt: int = 0
    completion: int = 0
    cached: int = 0


def _tracing_enabled() -> bool:
    return (
        os.environ.get("LANGSMITH_TRACING", "").strip().lower() == "true"
        and bool(os.environ.get("LANGSMITH_API_KEY", "").strip())
    )


class ArkClient:
    def __init__(self, settings: Settings | None = None, client: OpenAI | None = None):
        self.settings = settings or Settings.from_env()
        self._traced = False
        if client is None:
            client = OpenAI(
                base_url=self.settings.ark_base_url, api_key=self.settings.ark_api_key
            )
            if _tracing_enabled():
                # 延迟导入：未启用观测的环境（本地无 key）不该被 langsmith 报错拖住
                from langsmith.wrappers import wrap_openai

                client = wrap_openai(client)
                self._traced = True
        # client 可注入：测试用假对象，不碰网络也不需要 key
        self._client = client
        # 累计用量：跨多次调用汇总，进报告尾注；分模型才能按各自牌价算钱
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.usage_by_model: dict[str, ModelUsage] = {}

    def _record(self, model: str, usage) -> None:
        if not usage:
            return
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        book = self.usage_by_model.setdefault(model, ModelUsage())
        book.prompt += usage.prompt_tokens
        book.completion += usage.completion_tokens
        # 缓存命中字段是 OpenAI 兼容层的扩展信息，缺失时按零处理（不影响上限语义）
        details = getattr(usage, "prompt_tokens_details", None)
        book.cached += getattr(details, "cached_tokens", 0) or 0

    def _trace_kwargs(self, tags: list[str] | None) -> dict:
        # langsmith_extra 只有 wrap_openai 包装后的 create 认识，裸客户端会报未知参数
        if self._traced and tags:
            return {"langsmith_extra": {"tags": tags}}
        return {}

    def chat(
        self,
        *,
        model: str,
        system: str,
        user: str,
        thinking: bool = False,
        tags: list[str] | None = None,
    ) -> str:
        """普通文本补全。长文本生成（深读分析等）走这里而不是 tool call——
        实测方舟对长中文 tool arguments 的服务端解析会间歇性丢弃 tool_calls
        （finish_reason=tool_calls 但字段为空），纯文本没有 JSON 转义压力。
        """
        resp = self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}},
            temperature=0.3,
            **self._trace_kwargs(tags),
        )
        self._record(model, resp.usage)
        return (resp.choices[0].message.content or "").strip()

    def tool_call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        tool: dict,
        thinking: bool = False,
        max_attempts: int = 4,
        tags: list[str] | None = None,
    ) -> dict:
        """强制调用指定工具，返回解析后的 arguments dict。

        这里的重试针对「模型没好好返回」（缺 tool call / JSON 烂）——方舟实测
        存在间歇性丢 tool_calls 的服务端问题（约四成），4 次独立尝试把残余失败
        压到约 2.6%；网络层重试由 OpenAI SDK 自带。语义校验是调用方的事。
        """
        name = tool["function"]["name"]
        last_error = ""
        for _ in range(max_attempts):
            resp = self._client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                tools=[tool],
                tool_choice={"type": "function", "function": {"name": name}},
                # 方舟 thinking 开关（模型详情页 thinking.type）；OpenAI SDK 透传未知字段
                extra_body={"thinking": {"type": "enabled" if thinking else "disabled"}},
                temperature=0.2,  # 打分/抽取要求稳定复现，不要发散
                **self._trace_kwargs(tags),
            )
            self._record(model, resp.usage)
            calls = resp.choices[0].message.tool_calls
            if not calls:
                last_error = "模型未返回 tool call"
                continue
            try:
                return json.loads(calls[0].function.arguments)
            except json.JSONDecodeError:
                last_error = "tool call arguments 不是合法 JSON"
        raise ToolCallError(f"{last_error}（已尝试 {max_attempts} 次，model={model}）")

    def cost_summary(self) -> tuple[float, bool]:
        """返回（总成本元，是否精确价）。

        分模型按各自牌价计费，缓存命中部分按缓存价——这是平台既定计费行为，
        不是估算。「精确」只取决于 flash 牌价是否已配置：未配置时 flash 用量
        按 pro 价算，总额保持「上限估」语义（宁可高报不低报）。
        """
        s = self.settings
        precise = s.price_in_flash is not None and s.price_out_flash is not None
        total = 0.0
        for model, book in self.usage_by_model.items():
            is_flash = "flash" in model
            price_in = s.price_in_flash if (is_flash and precise) else s.price_in
            price_out = s.price_out_flash if (is_flash and precise) else s.price_out
            total += (
                (book.prompt - book.cached) * price_in
                + book.cached * s.price_cached
                + book.completion * price_out
            ) / 1e6
        return total, precise

    @property
    def cached_tokens(self) -> int:
        return sum(b.cached for b in self.usage_by_model.values())
