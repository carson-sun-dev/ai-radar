"""方舟 DeepSeek 客户端封装：强制 tool call 的一次往返。

三个存在理由：
- 结构化输出的唯一通道是 tool call——方舟的 DeepSeek-V4 不支持裸 JSON mode
  （模型详情页「结构化输出」未勾选），tool_choice 强制指定函数名保证必返回结构
- thinking 显式控制：思考 token 按输出价（24 元/M）计费，打分任务默认关闭，
  深读按需开启；不显式传参就把成本交给了平台默认值
- token 用量累计：P5 汇总进报告尾注（成本实测印在每份报告上）
"""

import json

from openai import OpenAI

from src.llm.settings import Settings


class ToolCallError(Exception):
    """模型未按要求返回 tool call，或 arguments 不是合法 JSON（重试耗尽后抛出）。"""


class ArkClient:
    def __init__(self, settings: Settings | None = None, client: OpenAI | None = None):
        self.settings = settings or Settings.from_env()
        # client 可注入：测试用假对象，不碰网络也不需要 key
        self._client = client or OpenAI(
            base_url=self.settings.ark_base_url, api_key=self.settings.ark_api_key
        )
        # 累计用量：跨多次调用汇总，P5 起进报告尾注
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def chat(
        self, *, model: str, system: str, user: str, thinking: bool = False
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
        )
        if resp.usage:
            self.prompt_tokens += resp.usage.prompt_tokens
            self.completion_tokens += resp.usage.completion_tokens
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
            )
            if resp.usage:
                self.prompt_tokens += resp.usage.prompt_tokens
                self.completion_tokens += resp.usage.completion_tokens
            calls = resp.choices[0].message.tool_calls
            if not calls:
                last_error = "模型未返回 tool call"
                continue
            try:
                return json.loads(calls[0].function.arguments)
            except json.JSONDecodeError:
                last_error = "tool call arguments 不是合法 JSON"
        raise ToolCallError(f"{last_error}（已尝试 {max_attempts} 次，model={model}）")
