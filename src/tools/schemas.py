"""OpenAI function 格式的工具 schema——单一事实来源。

LangGraph 节点内的调用与未来 MCP 封装（P9）共用这一份定义；
返回值必须再过 pydantic 校验（src/llm/scoring.py 等），schema 只约束形状不约束语义。
"""

# 两阶段漏斗第一阶段：批量打分。批量而非逐条，是为了摊薄 system prompt 的重复计费，
# 并让方舟隐式缓存命中固定前缀（1 元/M vs 12 元/M）
SUBMIT_SCORES_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_scores",
        "description": "对本批全部资讯逐条打分并归类板块。每条输入都必须有且仅有一条对应输出。",
        "parameters": {
            "type": "object",
            "properties": {
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "对应输入条目的 id，原样返回，不得编造",
                            },
                            "score": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 10,
                                "description": "对 AI 应用工程的可用性得分，依据 rubric",
                            },
                            "category": {
                                "type": "string",
                                "enum": ["model", "engineering", "paper"],
                                "description": "板块归属：模型动态/工程实践/论文新技术",
                            },
                            "reason": {
                                "type": "string",
                                "description": "一句话打分理由，中文",
                            },
                        },
                        "required": ["id", "score", "category", "reason"],
                    },
                }
            },
            "required": ["entries"],
        },
    },
}

# 注意：曾有 SUBMIT_ANALYSIS_TOOL 让深读分析走 tool call，已废弃——
# 长中文文本经 JSON 转义进 tool arguments 会触发方舟服务端间歇性丢弃 tool_calls，
# 长文本生成一律走普通 chat（见 src/llm/deepread.py 的通道选择说明）。

# 实体抽取：深读阶段（P4）与实体索引/历史关联（P6）共用。
# 实体是历史关联的检索键，命名要求可精确匹配（模型名带版本、技术名用通用写法）
EXTRACT_ENTITIES_TOOL = {
    "type": "function",
    "function": {
        "name": "extract_entities",
        "description": "从技术资讯中抽取可作检索键的实体（模型名、技术/方法名、框架名、机构名）。",
        "parameters": {
            "type": "object",
            "properties": {
                "entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "实体列表：模型名带版本（如 Qwen3.5-72B），"
                        "技术名用领域通用写法（如 KV cache 压缩）"
                    ),
                }
            },
            "required": ["entities"],
        },
    },
}


# 忠实度审查（P8，评测第 2 层，设计纪要第 15 节）：judge 对照原文核查深读分析里的
# 技术断言有无依据，返回 1-5 分与「无依据断言」清单。LLM-as-judge 的结构化出口。
SUBMIT_JUDGMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_judgment",
        "description": "对照原文核查分析的忠实度：打 1-5 分并列出无原文依据的技术断言。",
        "parameters": {
            "type": "object",
            "properties": {
                "score": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "忠实度：5=全部断言有据，3=个别存疑，1=大量臆造",
                },
                "unsupported": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "原文找不到依据的具体断言（数字/结论/方法），无则空数组",
                },
            },
            "required": ["score", "unsupported"],
        },
    },
}
