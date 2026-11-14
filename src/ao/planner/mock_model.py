"""无模型凭据时的确定性替身：仍然走完整编排，只是"节点清单"由规则给出。

用途：本机没有 `DEEPSEEK_API_KEY` / harness extra 时，页面也必须能跑通
"对话 → 草稿 → 编辑 → 发布"，否则 UI 无法验收。
"""

from __future__ import annotations

import re
from typing import Any

_REQUIREMENT_RE = re.compile(r"需求：\s*\n?(.+)")


def _subject(prompt: str) -> str:
    match = _REQUIREMENT_RE.search(prompt)
    raw = (match.group(1) if match else prompt).strip().splitlines()
    text = raw[0].strip() if raw else ""
    text = re.sub(r"[，。；,;：:].*$", "", text)
    return text[:16] or "每日简报"


class MockPlannerModel:
    """固定三段式：取数 → 整理 → 产出。"""

    async def generate(self, *, step: str, prompt: str) -> dict[str, Any]:
        subject = _subject(prompt)
        return {
            "nodes": [
                {
                    "id": "collect",
                    "title": "取数",
                    "kind": "agent",
                    "duty": f"收集与「{subject}」相关的原始信息",
                    "outputs": {"items": "str"},
                    "mcp": [],
                    "skills": [],
                },
                {
                    "id": "draft",
                    "title": "整理",
                    "kind": "agent",
                    "duty": f"把原始信息整理成「{subject}」的内容草稿",
                    "outputs": {"draft": "str"},
                    "mcp": [],
                    "skills": [],
                },
                {
                    "id": "finalize",
                    "title": "产出",
                    "kind": "agent",
                    "duty": "按目标格式输出最终结果",
                    "outputs": {"output": "str"},
                    "mcp": [],
                    "skills": [],
                },
            ],
            "edges": [
                {"from": "collect", "to": "draft"},
                {"from": "draft", "to": "finalize"},
            ],
        }
