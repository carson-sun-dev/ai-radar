# ai-radar

AI 前沿信息收集与摘要 agent：每周二/五（北京时间早 8 点）自动采集 10 家一线 AI 机构的动态与论文，生成中文周中报；周日汇总周报（Top 5 + 趋势 + 历史演进关联），邮件推送 PDF。目标读者是准备 AI 应用岗的自己——每周 20 分钟读完本周该知道的一切。

## 架构一览

- **运行时**：GitHub Actions（无服务器），仓库即数据库——报告、水位线状态、实体索引全部随 bot commit 入库
- **编排**：LangGraph（节点为纯函数），流水线：采集 → 去重 → 打分 → 深读 → 关联历史 → 校验 → 落盘/推送
- **LLM**：火山方舟 DeepSeek（OpenAI SDK 兼容），两阶段漏斗控制成本（标题+摘要打分 → top 条目深读）
- **溯源纪律**：URL/图片由数据层携带，模型只挑不写——从机制上杜绝引用幻觉
- **观测与评测**：LangSmith 全链路 trace（成本实测印进报告尾注）；规则校验 + LLM-as-judge 忠实度 + golden set 回归
- **信息源**：OpenAI、Anthropic、Google DeepMind、Hugging Face、LangChain、DeepSeek、Qwen、GLM、豆包/Seed、Kimi + arXiv 关键词

完整设计推演见 [docs/设计方案-协商纪要-2026-07-12.md](docs/设计方案-协商纪要-2026-07-12.md)，施工顺序见 [docs/施工计划.md](docs/施工计划.md)。

## 目录结构

```
src/
├── collectors/   # 四种采集器（RSS / GitHub API / arXiv / 网页抓取），重试 + 水位线
├── pipeline/     # LangGraph 图与节点（纯函数）
├── tools/        # OpenAI function 格式工具 schema（单一事实来源，MCP 复用）
├── llm/          # 方舟客户端封装、prompt 加载
├── report/       # md/JSON/PDF 渲染、SMTP 邮件
└── validate/     # 规则校验、忠实度 judge
prompts/          # prompt 模板（带设计意图注释）
data/             # [bot 通道] 水位线状态、实体索引、去重历史
reports/          # [bot 通道] 按月分目录的报告
assets/           # [bot 通道] 报告引用的图片
eval/             # golden.jsonl 与回归脚本
```

## 开发约定

- 代码走 PR（feature 分支 → CI 绿 → squash merge），main 禁止直推；`reports/`、`data/`、`assets/` 由 Actions bot 直推（ruleset bypass），commit 带 `[bot]` 前缀
- 分支命名 `feat/p<阶段号>-<简述>`，commit 遵循 Conventional Commits
- 注释用中文，写「为什么」不写「是什么」

## 施工进度

- [x] P0 仓库与骨架
- [x] P1 数据层（模型 / sources.yaml / 水位线与去重）
- [x] P2 采集器 ×4
- [ ] P3 LLM 层（方舟客户端 / 工具 schema / 打分 prompt）
- [ ] P4 最小闭环（周中报真实产出）
- [ ] P5 观测与规则校验（LangSmith）
- [ ] P6 周报与历史关联（实体索引）
- [ ] P7 推送与图片（PDF / SMTP）
- [ ] P8 评测深化（忠实度 judge / golden set）
- [ ] P9 MCP 化与 skills
- [ ] P10 上线与校准
