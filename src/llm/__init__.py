"""LLM 层：火山方舟 DeepSeek 客户端封装与 prompt 加载。

方舟 endpoint 兼容 OpenAI SDK：base_url 指向 https://ark.cn-beijing.volces.com/api/v3。
两阶段漏斗（设计纪要第 7 节）：第一阶段用标题+摘要打分（便宜），
第二阶段只对 top 条目抓全文深读（贵）——省的大头是输入 token。
"""
