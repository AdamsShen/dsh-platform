"""尖刀样例：手写的两节点工作流。

这份文件就是 P2 代码生成器要产出的形态：只 import wf_sdk，不内联提示词，
节点函数只做"取数 → 一次 turn → 契约解析"。
"""

from wf_sdk import agent, graph, node, parse_contract


@node(id="draft", outputs={"plan_path": str, "conflict_count": int}, title="生成日程")
async def draft(ctx):
    text = await agent()
    return parse_contract(text, ctx.contract)


@node(id="finalize", outputs={"ok": bool, "summary": str}, title="整理交付")
async def finalize(ctx):
    text = await agent()
    return parse_contract(text, ctx.contract)


G = graph(nodes=[draft, finalize], edges=[{"from": "draft", "to": "finalize"}])
