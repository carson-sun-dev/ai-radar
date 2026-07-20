"""评测第 2 层：忠实度 judge（LLM-as-judge，设计纪要第 15 节）。

用独立 prompt 对照清洗后原文核查深读分析的技术断言有无依据——与生成解耦，
避免模型「自己判自己」的宽松偏差。judge 复用深读已抓的 fulltext，不重新采集。

阈值语义：低于 FAITHFULNESS_MIN 触发一次重生成；重生成仍不过标「低置信」，
报告如实标注 ⚠ 而不是删条目——报告不说谎，把判断权交给读者。
"""

from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError

from src.llm.client import ArkClient, ToolCallError
from src.llm.prompts import load_prompt
from src.tools.schemas import SUBMIT_JUDGMENT_TOOL

FAITHFULNESS_MIN = 4  # 4-5 放行，≤3 判不过（个别关键断言无据即触发）
MAX_FULLTEXT_CHARS = 12000  # 与深读同口径，judge 输入不比深读看得更多


@dataclass
class Verdict:
    score: int
    unsupported: list[str] = field(default_factory=list)
    judged: bool = True  # judge 调用本身是否成功（失败时不冤枉分析，按放行处理）

    @property
    def passed(self) -> bool:
        # judge 没跑成不当作不忠实——避免 judge 抖动误伤正常分析（宁放过不错杀）
        return not self.judged or self.score >= FAITHFULNESS_MIN


class _Judgment(BaseModel):
    score: int = Field(ge=1, le=5)
    unsupported: list[str] = Field(default_factory=list)


def judge_faithfulness(client: ArkClient, analysis: str, fulltext: str) -> Verdict:
    """对照原文核查分析忠实度。judge 失败（tool call 烂/校验不过）返回 judged=False。"""
    user = f"【原文】\n{fulltext[:MAX_FULLTEXT_CHARS]}\n\n【分析】\n{analysis}"
    try:
        args = client.tool_call(
            model=client.settings.deepread_model,  # 核查要质量，走 pro
            system=load_prompt("judge"),
            user=user,
            tool=SUBMIT_JUDGMENT_TOOL,
            tags=["judge"],
        )
        result = _Judgment.model_validate(args)
    except (ToolCallError, ValidationError):
        return Verdict(score=0, judged=False)
    return Verdict(score=result.score, unsupported=result.unsupported)
