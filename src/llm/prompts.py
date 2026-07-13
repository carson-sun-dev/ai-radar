"""prompt 模板加载：prompts/*.md → 字符串。

模板里的 HTML 注释（<!-- ... -->）是给维护者看的设计意图，
加载时剥离——不该为注释付 token 钱，也不该让注释影响模型行为。
"""

import re
from functools import cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"
_HTML_COMMENT = re.compile(r"<!--.*?-->\s*", re.S)


@cache  # 同一次运行内容不变，没必要反复读盘
def load_prompt(name: str) -> str:
    text = (_PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    return _HTML_COMMENT.sub("", text).strip()
