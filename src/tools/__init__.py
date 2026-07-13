"""工具定义层：OpenAI function 格式的工具 schema（单一事实来源）。

所有结构化输出走 tool call 而非裸 JSON，返回后过 JSON Schema 校验。
schema 集中在本包，LangGraph 节点调用与未来 MCP 封装（P9）共用同一份定义。
"""
