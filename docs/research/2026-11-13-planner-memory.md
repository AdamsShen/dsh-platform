# 调研：创作会话的记忆与文档（P2-T11）

日期：2026-11-13
方法：web 检索（3 组关键词），范围有限，只覆盖"多轮状态管理 + 摘要压缩 + PDF 解析选型"三件事，
不涉及 UI 交互形态调研。结论只用于定 P2-T11 的数据结构，不给模型质量下判断。

## 1. 多轮对话状态：显式槽位 > 塞长上下文

来源：

- 华为云社区《多轮对话状态管理：别把"记住"完全交给上下文窗口》 https://bbs.huaweicloud.com/blogs/506582
- LeanMind《Conversational Memory That Works: Window, Summaries, and Pinned Facts》 https://leanmind.es/en/blog/conversational-memory-that-works-window-summaries-and-pinned-facts
- Medium《System Design of a Production WhatsApp AI Agent》 https://medium.com/@shivamhonrao2002/system-design-of-a-production-whatsapp-ai-agent-cheap-tokens-low-latency-full-recall-bae8c85f2874

共性做法是分三层：

1. **最近原文**：只留最近 3–5 轮，保证指代与语气连贯；不无限追加。
2. **结构化槽位**：业务关键实体抽成变量（budget / city / 时间频率…），显式存库，更新覆盖而不是追加。
3. **历史摘要**：更早的对话压成摘要，长期保留。

三篇来源都强调同一个失败模式：窗口变长不等于模型能稳定"记住并更新"旧值，用户改了需求但模型还在用旧槽位。

对本项目的取舍：槽位存 `planner_sessions.slots`（JSONB，按 key 存 `[{value, source, at}]`），
摘要存 `planner_sessions.summary`，`summarized_upto` 记录已压缩到的消息 id；最近消息按 id 倒序取 N 条。
不引入向量库——需求采集阶段的信息量远小于检索场景，关键信息必须结构化落库，否则后端生成步骤无法可靠引用。

## 2. LangGraph 的记忆分层（与本项目不冲突）

来源：

- machinelearningplus《LangGraph Memory: Short-Term, Long-Term & Conversation》 https://machinelearningplus.com/gen-ai/langgraph-memory-systems-short-long-term-conversation/
- CSDN《LangGraph 记忆模块实战：从线程记忆、长期记忆到对话摘要压缩》 https://agent.csdn.net/6a4e1eee662f9a54cb8b4078.html

LangGraph 用 checkpointer（线程级短期记忆，配 `thread_id`）+ store（跨线程长期记忆）两层，
并在 state 里用 `RemoveMessage` + 摘要消息控制上下文长度；生产环境换 `PostgresSaver` / `PostgresStore`。

本项目已经是"平台自己持有状态"（PG 表 + 文件），创作会话不走 LangGraph 图（它是长驻对话而非 DAG 执行），
所以只借鉴它的"摘要消息 + 最近原文"这个组合，不复用 checkpointer——否则会出现"会话状态在
LangGraph 里、业务槽位在 PG 里"的双写不一致。

## 3. 文档解析：先 pypdf，别过早上 PyMuPDF

来源：

- The Neural Base《pypdf vs pdfplumber》 https://theneuralbase.com/compare/pypdf-vs-pdfplumber/
- DigiParser《Extract Text from PDF in Python: A 2026 Guide》 https://www.digiparser.com/blog/extract-text-from-pdf-in-python
- Subhajit Bhar《pdfplumber vs PyMuPDF vs pypdf》 https://subhajitbhar.com/blog/pdf-extraction/pdfplumber-vs-pymupdf-vs-pypdf2/

结论：

- pypdf 纯 Python、装起来最轻，适合"数字版 PDF 的简单文本"；比 pdfplumber 快约 4 倍，但不保版式、表格弱。
- pdfplumber 依赖 pdfminer.six + Pillow，复杂版式更准，大文件慢（500 页级别到 5–10 秒/页）。
- PyMuPDF 抽取质量与速度最好，但体积大、许可证是 AGPL（对商业分发有约束）。
- 扫描件（无文本层）任何库都抽不出文字，需要 OCR，属于另一个量级。

对本项目的取舍：需求文档以 `.md` / `.txt` / `.pdf` 为主，先只支持这几类 + `.json/.csv/.yaml`；
PDF 用 pypdf，抽不出文本时**明确报错**让人改传 md，不静默产出空槽位。扫描件与表格解析本轮不做。

## 4. 落到 P2-T11 的设计结论

| 决策 | 依据 |
|---|---|
| 槽位结构化存库，可追溯来源（user / upload:文件名） | 第 1 节：显式状态比长上下文可靠；生成步骤要能引用"哪份文档" |
| 摘要按批压缩（攒够 N 条压最早 M 条），不做逐轮摘要 | 逐轮摘要成本高且易漂移；批次压缩才有稳定边界 |
| 摘要里带当时的槽位快照 | 只保留原文摘要会丢"用户在第 3 轮改过频率"这类事实 |
| 文档先落盘（`uploads/<session>/<sha256>.<ext>`）再抽文本 | 会话可重开、可复现；文件名不可信，按内容寻址 |
| 上传大小/类型白名单 + 文件名去路径 | 安全基线：`../` 穿越与超大文件必须挡在入口 |
| 解析失败直接报错 | 本项目原则"不静默降级"，空文本会让后续生成看起来成功实则缺信息 |

未覆盖：多模态图片、OCR、跨会话检索（后续若需要再评估向量方案）。
