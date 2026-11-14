"""创作会话：一个对话页 = 一个 `planner_session`。

职责边界：
- 消息与附件的持久化（可崩溃重开，不做接管）；
- 槽位与滚动摘要的读写（纯函数在 `memory.py`）；
- 会话级 token 记账（科目 `authoring`，不进 run 成本）。

不做的事：不调模型、不生成包、不管 UI。
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from ao import usage
from ao.db import fetch_all, fetch_one, tx
from ao.planner import documents, memory
from ao.settings import settings
from ao.util import new_id

MAX_RECENT_MESSAGES = 8


def create_session(
    *, tenant_id: str, user_id: str, workflow_id: str | None = None
) -> dict[str, Any]:
    session_id = new_id("planner")
    with tx() as conn:
        row = conn.execute(
            "INSERT INTO planner_sessions (id, tenant_id, user_id, workflow_id)"
            " VALUES (%s,%s,%s,%s) RETURNING *",
            (session_id, tenant_id, user_id, workflow_id),
        ).fetchone()
    return row


def get_session(session_id: str) -> dict[str, Any] | None:
    return fetch_one("SELECT * FROM planner_sessions WHERE id = %s", (session_id,))


def require_session(session_id: str) -> dict[str, Any]:
    session = get_session(session_id)
    if session is None:
        raise LookupError(f"planner session 不存在: {session_id}")
    return session


def bind_workflow(session_id: str, workflow_id: str) -> None:
    """发布成功时回填：会话与产出物建立可追溯关系。"""
    with tx() as conn:
        conn.execute(
            "UPDATE planner_sessions SET workflow_id = %s, updated_at = now() WHERE id = %s",
            (workflow_id, session_id),
        )


def set_stage(
    session_id: str, stage: str, issues: list[dict[str, Any]] | None = None
) -> None:
    """推进创作阶段；失败时把 issues 一起落库，页面据此展示原因。"""
    with tx() as conn:
        if issues is None:
            conn.execute(
                "UPDATE planner_sessions SET stage = %s, updated_at = now() WHERE id = %s",
                (stage, session_id),
            )
        else:
            conn.execute(
                "UPDATE planner_sessions SET stage = %s, draft_issues = %s, updated_at = now()"
                " WHERE id = %s",
                (stage, Jsonb(issues), session_id),
            )


def _merge_slots(
    session_id: str, extracted: dict[str, list[str]], *, source: str
) -> dict[str, Any]:
    with tx() as conn:
        row = conn.execute(
            "SELECT slots FROM planner_sessions WHERE id = %s FOR UPDATE", (session_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"planner session 不存在: {session_id}")
        merged = memory.merge_slots(row["slots"], extracted, source=source)
        conn.execute(
            "UPDATE planner_sessions SET slots = %s, updated_at = now() WHERE id = %s",
            (Jsonb(merged), session_id),
        )
    return merged


def append_message(
    session_id: str,
    role: str,
    content: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """落一条消息；用户消息顺带抽槽位，然后按阈值尝试压缩历史。"""
    with tx() as conn:
        row = conn.execute(
            "INSERT INTO planner_messages (session_id, role, content, attachments, payload)"
            " VALUES (%s,%s,%s,%s,%s) RETURNING *",
            (
                session_id,
                role,
                content,
                Jsonb(attachments or []),
                Jsonb(payload or {}),
            ),
        ).fetchone()
        conn.execute(
            "UPDATE planner_sessions SET updated_at = now() WHERE id = %s", (session_id,)
        )

    if role == "user" and content:
        _merge_slots(session_id, memory.extract_slots(content), source="user")
    maybe_summarize(session_id)
    return row


def list_messages(session_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    rows = fetch_all(
        "SELECT * FROM planner_messages WHERE session_id = %s ORDER BY id DESC LIMIT %s",
        (session_id, limit),
    )
    return list(reversed(rows))


def upload_document(session_id: str, filename: str, data: bytes) -> dict[str, Any]:
    """上传文档：落盘 → 抽文本 → 记消息 → 槽位记录文件名（生成时可引用）。"""
    require_session(session_id)
    attachment = documents.save_upload(session_id, filename, data)
    message = append_message(
        session_id,
        "user",
        f"[上传文档] {attachment['name']}\n{attachment['excerpt'][:500]}",
        attachments=[
            {key: value for key, value in attachment.items() if key != "text" and key != "excerpt"}
        ],
    )
    # documents 槽位单独记：生成步骤要知道"需求来自哪几份文件"
    _merge_slots(
        session_id,
        {"documents": [attachment["name"]]},
        source=f"upload:{attachment['name']}",
    )
    attachment["message_id"] = message["id"]
    return attachment


def maybe_summarize(session_id: str) -> bool:
    """未压缩消息攒够阈值就压最早一批；返回是否真的压了。

    阈值与批量来自 settings：逐轮压缩成本高，且摘要会随着轮次漂移。
    """
    session = require_session(session_id)
    pending = fetch_all(
        "SELECT id, role, content FROM planner_messages"
        " WHERE session_id = %s AND id > %s AND role IN ('user','assistant') ORDER BY id",
        (session_id, session["summarized_upto"]),
    )
    if len(pending) < settings.planner_summary_after:
        return False
    batch = pending[: max(1, settings.planner_summary_batch)]

    # 摘要里带"当时"的槽位快照：只留原文会丢掉"第 3 轮改过频率"这类事实
    batch_slots: dict[str, Any] = {}
    for message in batch:
        batch_slots = memory.merge_slots(
            batch_slots, memory.extract_slots(message["content"]), source=str(message["role"])
        )

    digest = memory.summary_digest(batch, batch_slots)
    summary = "\n".join(part for part in [session["summary"], digest] if part)
    with tx() as conn:
        conn.execute(
            "UPDATE planner_sessions SET summary = %s, summarized_upto = %s, updated_at = now()"
            " WHERE id = %s",
            (summary, batch[-1]["id"], session_id),
        )
    return True


def build_context(session_id: str, *, recent: int = MAX_RECENT_MESSAGES) -> dict[str, Any]:
    """给下一步（澄清/节点清单）的上下文：摘要 + 槽位 + 最近原文 + 文档清单。"""
    session = require_session(session_id)
    slots = session["slots"] or {}
    return {
        "session_id": session_id,
        "summary": session["summary"],
        "slots": slots,
        "slots_text": memory.render_slots(slots),
        "documents": memory.slot_values(slots, "documents"),
        "messages": list_messages(session_id, limit=recent),
    }


def record_authoring_usage(
    session_id: str, usage_data: dict[str, Any], *, gateway_request_id: str | None = None
) -> float:
    """会话 token 计入 platform.authoring：run_id 为空，node_id 存会话 id。"""
    with tx() as conn:
        return usage.record(
            conn,
            run_id=None,
            node_id=session_id,
            attempt=0,
            usage=usage_data,
            purpose="authoring",
            gateway_request_id=gateway_request_id,
        )


def close_session(session_id: str, status: str = "closed") -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE planner_sessions SET status = %s, updated_at = now() WHERE id = %s",
            (status, session_id),
        )
