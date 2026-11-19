"""创作面对话页：对话采集 → 生成草稿 → 人工编辑 → 确认发布。

A 期不引前端构建链：服务端渲染 + 表单 PRG（POST-Redirect-GET）。
生成是同步调用（mock 秒级；真实模型单轮 10s 级，先接受阻塞，B 期改后台任务 + 进度轮询）。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ao import resources
from ao import triggers as trigger_store
from ao import users as users_store
from ao.cron import timezone_of
from ao.db import fetch_all
from ao.planner import draft as draft_store
from ao.planner import orchestrator
from ao.planner import session as planner_session
from ao.planner.mock_model import MockPlannerModel
from ao.settings import settings

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app() -> FastAPI:
    app = FastAPI(title="AO 创作面")

    def _user(request: Request) -> str:
        """A 期没有登录，先用请求头标识身份；缺省给一个演示用户。"""
        return request.headers.get("X-User-Id") or "u_001"

    def _model() -> Any:
        # 没有 key 时也要能验收页面：mock 模型同样走七步编排
        if settings.mock:
            return MockPlannerModel()
        return orchestrator.DshPlannerModel()

    def _artifacts(session_id: str) -> list[dict[str, Any]]:
        rows = fetch_all(
            "SELECT payload FROM planner_messages WHERE session_id = %s AND role = 'artifact'"
            " ORDER BY id",
            (session_id,),
        )
        return [row["payload"] for row in rows]

    def _render(request: Request, session_id: str) -> HTMLResponse:
        session = planner_session.require_session(session_id)
        context = planner_session.build_context(session_id, recent=40)
        nodes = (
            draft_store.editable_view(draft_store.Draft.load(session_id))
            if draft_store.exists(session_id)
            else []
        )
        issues = session.get("draft_issues") or []
        draft = draft_store.Draft.load(session_id) if nodes else None
        selected = {
            f"{item['kind']}:{item['ref']}" for item in (draft.bindings if draft else [])
        }
        schedule = {
            "cron": "",
            "timezone": "Asia/Shanghai",
            "catchup_window_seconds": 3600,
            "catchup_limit": 1,
            "overlap_policy": "skip",
        }
        if draft and draft.triggers:
            first = draft.triggers[0]
            schedule = {
                "cron": str(first.get("cron") or ""),
                "timezone": str(first.get("timezone") or "Asia/Shanghai"),
                "catchup_window_seconds": first.get("catchup_window_seconds") or 3600,
                "catchup_limit": first.get("catchup_limit") or 1,
                "overlap_policy": str(first.get("overlap_policy") or "skip"),
            }
        # 已发布的计划以投影表为准：草稿里的 cron 只是"待发布"的意图
        plans = (
            trigger_store.for_workflow(draft.workflow_id) if draft and draft.workflow_id else []
        )
        row = next(
            (item for item in plans if item["enabled"] and item["type"] == "cron"), None
        )
        active = None
        if row:
            active = {
                "id": row["id"],
                "cron": row["cron"],
                "timezone": row["timezone"],
                "overlap_policy": row.get("overlap_policy") or "skip",
                "next_local": row["next_fire_at"]
                .astimezone(timezone_of(row["timezone"]))
                .strftime("%Y-%m-%d %H:%M"),
                # 缓冲槽位：页面要能看出"有一次待跑还没消费"
                "pending_local": (
                    row["pending_fire_at"]
                    .astimezone(timezone_of(row["timezone"]))
                    .strftime("%Y-%m-%d %H:%M")
                    if row.get("pending_fire_at")
                    else ""
                ),
            }
        event_plans = [item for item in plans if item["enabled"] and item["type"] == "event"]
        return templates.TemplateResponse(
            request,
            "planner.html",
            {
                "session": session,
                "stage": session.get("stage") or "chatting",
                "messages": [m for m in context["messages"] if m["role"] != "artifact"],
                "slots": context["slots"],
                "slots_text": context["slots_text"],
                "nodes": nodes,
                "available": (
                    resources.list_available(user_id=session["user_id"]) if nodes else []
                ),
                "selected": selected,
                "schedule": schedule,
                "active_trigger": active,
                "event_plans": event_plans,
                "workflow_id": (draft.workflow_id if draft else "") or "",
                "artifacts": _artifacts(session_id),
                "issues": issues,
            },
        )

    async def _edits(request: Request, session_id: str) -> list[dict[str, Any]]:
        """从编辑表单还原节点改动；字段名按 `title_<node_id>` 约定。"""
        form = await request.form()
        draft = draft_store.Draft.load(session_id)
        edits: list[dict[str, Any]] = []
        for node in draft.nodes:
            node_id = node["id"]
            if f"title_{node_id}" not in form:
                continue
            raw_outputs = str(form.get(f"outputs_{node_id}") or "{}")
            try:
                outputs: Any = json.loads(raw_outputs)
            except json.JSONDecodeError:
                # 交给 apply_edits 报 E_OUTPUTS，页面能看到具体是哪一项
                outputs = raw_outputs
            edits.append(
                {
                    "id": node_id,
                    "title": form.get(f"title_{node_id}"),
                    "duty": form.get(f"duty_{node_id}"),
                    "prompt": form.get(f"prompt_{node_id}"),
                    "system": form.get(f"system_{node_id}"),
                    "outputs": outputs,
                    # 未勾选 = 该节点不绑定（不是"继承"，继承由不提交字段表达；
                    # 页面永远是全量勾选视图，所以这里必须按"当前勾选即真相"处理）
                    "mcp": [str(item) for item in form.getlist(f"mcp_{node_id}")],
                    "skills": [str(item) for item in form.getlist(f"skills_{node_id}")],
                }
            )
        return edits

    @app.get("/", response_class=HTMLResponse)
    async def index() -> RedirectResponse:
        return RedirectResponse("/planner/new", status_code=303)

    @app.get("/planner/new")
    async def new_session(request: Request) -> RedirectResponse:
        session = planner_session.create_session(
            tenant_id=settings.default_tenant, user_id=_user(request)
        )
        return RedirectResponse(f"/planner/{session['id']}", status_code=303)

    @app.get("/planner/{session_id}", response_class=HTMLResponse)
    async def show(request: Request, session_id: str) -> HTMLResponse:
        try:
            return _render(request, session_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/planner/{session_id}/messages")
    async def post_message(request: Request, session_id: str) -> RedirectResponse:
        form = await request.form()
        content = str(form.get("content") or "").strip()
        if content:
            planner_session.append_message(session_id, "user", content)
        return RedirectResponse(f"/planner/{session_id}", status_code=303)

    @app.post("/planner/{session_id}/uploads")
    async def upload(
        request: Request, session_id: str, file: UploadFile
    ) -> RedirectResponse:
        data = await file.read()
        try:
            planner_session.upload_document(session_id, file.filename or "upload.txt", data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(f"/planner/{session_id}", status_code=303)

    @app.post("/planner/{session_id}/generate")
    async def generate(session_id: str) -> RedirectResponse:
        await orchestrator.prepare(session_id, model=_model())
        return RedirectResponse(f"/planner/{session_id}", status_code=303)

    @app.post("/planner/{session_id}/nodes")
    async def save_nodes(request: Request, session_id: str) -> RedirectResponse:
        await orchestrator.update_draft(session_id, await _edits(request, session_id))
        return RedirectResponse(f"/planner/{session_id}", status_code=303)

    @app.post("/planner/{session_id}/bindings")
    async def save_bindings(request: Request, session_id: str) -> RedirectResponse:
        """工具绑定是工作流级的：提交的勾选项直接覆盖。"""
        form = await request.form()
        session = planner_session.require_session(session_id)
        available = {
            f"{row['kind']}:{row['ref']}": row
            for row in resources.list_available(user_id=session["user_id"])
        }
        bindings = [
            {
                "kind": available[key]["kind"],
                "ref": available[key]["ref"],
                "scope": available[key]["scope"],
            }
            for key in form.getlist("binding")
            if str(key) in available
        ]
        await orchestrator.update_draft(session_id, [], bindings=bindings)
        return RedirectResponse(f"/planner/{session_id}", status_code=303)

    @app.post("/planner/{session_id}/schedule")
    async def save_schedule(request: Request, session_id: str) -> RedirectResponse:
        """定时计划：cron 留空表示取消定时；保存只重建草稿，发布后才真正生效。"""
        form = await request.form()
        await orchestrator.update_draft(
            session_id,
            [],
            schedule={
                "cron": str(form.get("cron") or ""),
                "timezone": str(form.get("timezone") or "Asia/Shanghai"),
                "catchup_window_seconds": form.get("catchup_window_seconds") or 3600,
                "catchup_limit": form.get("catchup_limit") or 1,
                "overlap_policy": str(form.get("overlap_policy") or "skip"),
            },
        )
        return RedirectResponse(f"/planner/{session_id}", status_code=303)

    @app.post("/planner/{session_id}/publish")
    async def publish_draft(request: Request, session_id: str) -> RedirectResponse:
        # 先落编辑再发布：两个按钮共用一个表单，避免"改了没保存就发"
        updated = await orchestrator.update_draft(session_id, await _edits(request, session_id))
        if updated.ok:
            await orchestrator.publish_draft(session_id)
        return RedirectResponse(f"/planner/{session_id}", status_code=303)

    def _event_allowed(request: Request) -> bool:
        """事件入站鉴权：配了令牌就必须匹配；没配时只在 dev 放行。

        生产环境裸开等于给所有人一个"点火"按钮，宁可拒绝也不默认放行。
        """
        if settings.event_token:
            return request.headers.get("X-AO-Event-Token") == settings.event_token
        return settings.env == "dev"

    @app.post("/api/workflows/{workflow_id}/events/{event_name}")
    async def receive_event(
        request: Request, workflow_id: str, event_name: str
    ) -> dict[str, Any]:
        """外部系统触发工作流：上游跑完、业务 webhook 都打这里。

        幂等：`X-AO-Event-Id` 相同只落一个 run；不带则每次新 run。
        没有匹配计划时返回 200 + ignored，避免对端把 4xx 当失败反复重试。
        """
        if not _event_allowed(request):
            raise HTTPException(status_code=401, detail="事件令牌不正确或未配置")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — 允许空 body（纯 ping 式触发）
            body = {}
        if not isinstance(body, dict):
            body = {"value": body}
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
        result = trigger_store.emit_event(
            workflow_id=workflow_id,
            event_name=event_name,
            payload=payload,
            idempotency_key=request.headers.get("X-AO-Event-Id")
            or str(body.get("id") or ""),
        )
        return {"ok": True, **result}

    def _parse_instant(value: Any) -> datetime:
        """解析 ISO8601 时间；不带时区直接拒绝，避免把本地时间当 UTC 用。"""
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("start/end 必填（ISO8601，建议带 Z）")
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError(f"时间必须带时区: {raw!r}（如 2026-11-01T00:00:00Z）")
        return moment.astimezone(UTC)

    @app.post("/api/workflows/{workflow_id}/triggers/{trigger_id}/backfill")
    async def backfill_trigger(
        request: Request, workflow_id: str, trigger_id: str
    ) -> dict[str, Any]:
        """手动补跑：把 [start, end] 内的 cron 槽位逐个建成 run。

        幂等键与正常触发一致 → 重复调用（或某时刻已正常跑过）返回 `created=false`；
        不改 `next_fire_at`，补历史不影响未来计划；回填忽略重叠检查。
        """
        if not _event_allowed(request):
            raise HTTPException(status_code=401, detail="事件令牌不正确或未配置")
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001 — 参数缺失时给出可读的 400
            raise HTTPException(status_code=400, detail="body 必须是 JSON 对象") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")
        try:
            start = _parse_instant(body.get("start"))
            end = _parse_instant(body.get("end"))
            limit = int(body.get("limit") or trigger_store.DEFAULT_BACKFILL_LIMIT)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            result = trigger_store.backfill(
                workflow_id=workflow_id,
                trigger_id=trigger_id,
                start=start,
                end=end,
                limit=limit,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    def _ops_allowed(request: Request) -> bool:
        """运维接口鉴权：与事件入站同构（令牌 + dev 兜底）。

        P3-T02 的账号/API Key 体系未落地前，主体目录的读写只开放给运维：
        生产必须配 `AO_OPS_TOKEN`，dev 允许本机直接调。
        """
        if settings.ops_token:
            return request.headers.get("X-AO-Ops-Token") == settings.ops_token
        return settings.env == "dev"

    @app.get("/api/users")
    def list_users(request: Request, tenant_id: str | None = None) -> dict[str, Any]:
        """列出可被扇出的主体（运维排查用；不返回 password_hash 等敏感列）。"""
        if not _ops_allowed(request):
            raise HTTPException(status_code=401, detail="运维令牌不正确或未配置")
        return {"ok": True, "users": users_store.list_active_users(tenant_id)}

    @app.post("/api/users")
    async def upsert_user(request: Request) -> dict[str, Any]:
        """同步一个主体到引擎侧目录（业务系统推送或人工补录）。

        `external_ref` 是业务主键，重复写入按 (tenant, external_ref) 幂等更新。
        """
        if not _ops_allowed(request):
            raise HTTPException(status_code=401, detail="运维令牌不正确或未配置")
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — 非 JSON 一律按 400 处理
            raise HTTPException(status_code=400, detail="请求体必须是 JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求体必须是对象")
        try:
            row = users_store.upsert(
                tenant_id=body.get("tenant_id"),
                external_ref=str(body.get("external_ref") or ""),
                display_name=body.get("display_name"),
                status=str(body.get("status") or "active"),
            )
        except users_store.UserError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "user": {
                "id": row["id"],
                "external_ref": row["external_ref"],
                "status": row["status"],
            },
        }

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "mock": settings.mock, "env": settings.env}

    return app


app = create_app()
