"""阶段记忆：跨阶段只靠文件，不靠会话上下文。

对齐 sortie-deck 的 `mission_memory`：会话是可丢弃的，记忆在磁盘上。
- `memory.md`：每个阶段收尾时由 LLM 提炼 3-5 行追加，滚动保留最近一段；
- `artifacts/`：阶段产物的权威来源，索引进下一阶段的 prompt；
- 上游节点的结构化产物仍从 LangGraph state 注入（模板里可直接引用）。

新会话开场由 `bootstrap_block()` 拼进首轮 prompt，阶段之间零会话继承。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

#: memory.md 进入 prompt 时最多保留的尾部字符数，避免长任务把上下文吃满
MEMORY_CHARS = 4000
#: 单个上游节点产物写进 bootstrap 时的截断长度
UPSTREAM_CHARS = 800
#: 产物索引最多列出的文件数
ARTIFACT_LIMIT = 50

MEMORY_FILE = "memory.md"


def memory_path(package_root: Path) -> Path:
    return package_root / MEMORY_FILE


def load_memory(package_root: Path, limit: int = MEMORY_CHARS) -> str:
    """读滚动摘要；只取尾部，超出部分按整段丢弃而不是拦腰截断。"""
    path = memory_path(package_root)
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    if len(text) <= limit:
        return text.strip()
    tail = text[-limit:]
    # 从下一个二级标题开始，避免把某个阶段摘要切成两半
    marker = tail.find("\n## ")
    return (tail[marker + 1 :] if marker >= 0 else tail).strip()


def append_stage_summary(package_root: Path, node_id: str, summary: str) -> None:
    """追加一个阶段的摘要。

    只写不读回（读的是 `load_memory`），因此并发节点会各写各的段落；
    末尾统一补一个换行，避免下一次追加粘在上一行。
    """
    text = (summary or "").strip()
    if not text:
        return
    stamp = datetime.now().isoformat(timespec="seconds")
    path = memory_path(package_root)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## 阶段 {node_id}（{stamp}）\n{text}\n")


def artifact_index(package_root: Path, limit: int = ARTIFACT_LIMIT) -> list[str]:
    """列出已产出的文件（包内相对路径），按修改时间倒序。"""
    artifacts = package_root / "artifacts"
    if not artifacts.is_dir():
        return []
    files = [path for path in artifacts.rglob("*") if path.is_file()]
    files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return [str(path.relative_to(package_root)) for path in files[:limit]]


def _short(value: Any, limit: int = UPSTREAM_CHARS) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def bootstrap_block(
    package_root: Path, node_id: str, nodes: dict[str, dict[str, Any]]
) -> str:
    """拼本阶段的"开场记忆"：摘要 + 上游产物 + 产物索引。

    返回空串表示没有任何可注入的内容（首个阶段且无产物）。
    """
    sections: list[str] = []
    memory = load_memory(package_root)
    if memory:
        sections.append(f"## 前序阶段记忆\n{memory}")

    upstream = [
        f"- {name}: {_short(value)}" for name, value in nodes.items() if name != node_id
    ]
    if upstream:
        sections.append("## 上游阶段产物\n" + "\n".join(upstream))

    files = artifact_index(package_root)
    if files:
        sections.append("## 已产出文件\n" + "\n".join(f"- {name}" for name in files))

    if not sections:
        return ""
    return (
        "以下是本阶段开始前已有的任务记忆与产物。"
        "会话上下文不会跨阶段延续，需要更多细节请直接读文件。\n\n"
        + "\n\n".join(sections)
    )


def summary_prompt(node_id: str, data: dict[str, Any]) -> str:
    """阶段收尾的摘要指令：只留决策与结论，不复述产物。"""
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    if len(payload) > 4000:
        payload = payload[:4000] + "\n…（已截断）"
    return (
        f"阶段「{node_id}」已结束，产物如下。\n"
        "请输出 3-5 行 markdown 摘要，只写：做了什么决定、结论是什么、下游要注意什么。\n"
        "不要复述 JSON，不要客套话，不要代码块。\n\n"
        f"{payload}"
    )
