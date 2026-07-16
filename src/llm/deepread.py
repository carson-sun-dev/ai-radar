"""两阶段漏斗第二阶段：深读与中读（pro 模型，设计纪要第 7 节）。

深读抓全文（Jina Reader 渲染+清洗一步到位），中读只用标题+摘要——
输入 token 是成本大头，全文只给配得上它的条目。

失败语义：分析失败不阻塞报告，item.analysis 留空，渲染层降级用摘要+打分理由。

通道选择（方舟实测教训）：长中文分析走普通 chat 补全——塞进 tool call 的
JSON 参数会触发方舟服务端的间歇性解析丢弃（越长越容易丢）；实体抽取输出短，
走 tool call 拿结构化结果。「什么时候用 function calling」的边界就在这里。
"""

from pydantic import ValidationError

from src.collectors import base
from src.config import RetryDefaults
from src.llm.client import ArkClient, ToolCallError
from src.llm.prompts import load_prompt
from src.models import NewsItem
from src.tools.schemas import EXTRACT_ENTITIES_TOOL

# 约 8k token：单条深读输入的成本上限，超长文章截断（关键内容通常在前部）
MAX_FULLTEXT_CHARS = 12000


def fetch_fulltext(url: str, retry: RetryDefaults) -> str | None:
    """拉正文。失败返回 None 而不抛异常：深读降级用摘要，不值得废掉整条条目。"""
    try:
        return base.fetch(f"https://r.jina.ai/{url}", retry)[:MAX_FULLTEXT_CHARS]
    except base.FetchError:
        return None


def analyze_item(
    client: ArkClient, item: NewsItem, prompt_name: str, fulltext: str | None = None
) -> bool:
    """深读（prompt_name='deepread'，带全文）或中读（'midread'，只有摘要）。

    成功则就地填充 item.analysis / item.entities，返回是否成功。
    实体抽取失败不算整体失败：分析文本比实体列表值钱得多。
    """
    parts = [
        f"标题: {item.title}",
        f"来源: {item.source}",
        f"发布: {item.published_at.date()}",
        f"摘要: {item.summary or '（无）'}",
    ]
    if fulltext:
        parts.append(f"正文:\n{fulltext}")
    # trace 标签带板块/源（P5）：LangSmith 里按标签切片看各板块/各源的成本与耗时
    tags = [prompt_name, item.source] + ([item.category.value] if item.category else [])
    analysis = client.chat(
        model=client.settings.deepread_model,
        system=load_prompt(prompt_name),
        user="\n".join(parts),
        tags=tags,
    )
    # 防御模型偶发的格式自作主张：代码块围栏剥掉；整体是 JSON 的按失败处理
    analysis = analysis.removeprefix("```markdown").removeprefix("```").removesuffix("```").strip()
    if len(analysis) < 50 or analysis.startswith("{"):
        return False  # 触发渲染层降级（摘要顶上），如实标注
    item.analysis = analysis

    # 实体：小 tool call，输入用「标题+分析」而不是原文——分析里的实体密度更高
    try:
        args = client.tool_call(
            model=client.settings.score_model,
            system="从给定文本中抽取实体，调用 extract_entities 提交。",
            user=f"{item.title}\n\n{analysis}",
            tool=EXTRACT_ENTITIES_TOOL,
            tags=["entities", item.source],
        )
        entities = args.get("entities", [])
        if isinstance(entities, list):
            # 只收字符串：模型偶尔违反 schema 返回对象，str(dict) 会污染索引键
            item.entities = [e for e in entities if isinstance(e, str)][:10]
    except (ToolCallError, ValidationError):
        pass  # 实体缺了不影响报告主体，P6 索引少一条而已
    return True
