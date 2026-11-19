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
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ao import audit, resources
from ao import runs as runs_store
from ao import share as share_store
from ao import state as run_state
from ao import triggers as trigger_store
from ao import users as users_store
from ao.cron import timezone_of
from ao.db import fetch_all, fetch_one
from ao.pages import compiler as page_compiler
from ao.pages import datasources as page_data
from ao.pages import editor as page_editor
from ao.pages import hosting as page_hosting
from ao.pages import nl_edit as page_nl
from ao.pages import schema as page_schema
from ao.pages import share_view as page_share_view
from ao.pages.schema import ACTIONS as PAGE_ACTIONS
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

    def _draft_root(session_id: str) -> Path:
        if not draft_store.exists(session_id):
            raise HTTPException(status_code=404, detail=f"会话没有草稿: {session_id}")
        return draft_store.draft_dir(session_id)

    @app.get("/planner/{session_id}/pages/{page_id}", response_class=HTMLResponse)
    def page_editor_view(
        request: Request, session_id: str, page_id: str, error: str | None = None
    ) -> HTMLResponse:
        """页面编辑器：树 + 属性面板 + 即时预览。"""
        root = _draft_root(session_id)
        schema = page_editor.load_page(root, page_id)
        return templates.TemplateResponse(
            request,
            "page_editor.html",
            {
                "session_id": session_id,
                "page_id": page_id,
                "schema": schema,
                "error": error or "",
                "components": sorted(page_schema.COMPONENTS),
                "actions": sorted(page_schema.ACTIONS),
                "formats": sorted(page_schema.COLUMN_FORMATS),
            },
        )

    @app.post("/planner/{session_id}/pages/{page_id}/op")
    async def page_editor_op(
        request: Request, session_id: str, page_id: str
    ) -> RedirectResponse:
        """一次编辑 = 一次提交；失败回编辑器并把原因带回去，不写半成品。"""
        root = _draft_root(session_id)
        form = await request.form()
        op = str(form.get("op") or "")
        try:
            target = int(str(form.get("target") or 0))
        except ValueError:
            target = 0
        payload = {
            key: str(form.get(key) or "") for key in ("field", "label", "format", "value", "node")
        }
        if form.get("confirm"):
            payload["confirm"] = "1"
        base = f"/planner/{session_id}/pages/{page_id}"
        try:
            schema = page_editor.load_page(root, page_id)
            updated = page_editor.apply_op(schema, op, target, payload)
            page_editor.save_page(root, page_id, updated)
        except page_editor.EditorError as exc:
            return RedirectResponse(f"{base}?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(base, status_code=303)

    @app.post("/planner/{session_id}/pages/{page_id}/nl")
    async def page_editor_nl(
        request: Request, session_id: str, page_id: str
    ) -> RedirectResponse:
        """一句话改页面：解析成同一套编辑操作，落盘仍走校验。

        不调模型：加列/删列/加按钮的语法稳定，用规则解析比模型更可控；
        解析不出来就明确告诉用户"没听懂"，不猜一个改动出去。
        """
        root = _draft_root(session_id)
        form = await request.form()
        text = str(form.get("text") or "").strip()
        outcome = page_nl.apply_text(root, page_id, text)
        base = f"/planner/{session_id}/pages/{page_id}"
        if not outcome["ok"]:
            return RedirectResponse(f"{base}?error={quote(outcome['message'])}", status_code=303)
        return RedirectResponse(f"{base}?ok={quote(outcome['message'])}", status_code=303)

    @app.get("/planner/{session_id}/pages/{page_id}/preview/{asset:path}")
    def page_editor_preview(session_id: str, page_id: str, asset: str) -> FileResponse:
        """预览静态产物：从草稿目录读，草稿未编译时 404（不现场编译）。"""
        root = _draft_root(session_id)
        try:
            path = page_hosting.resolve_asset(root, page_id, asset)
        except page_hosting.HostingError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            path,
            headers={
                "Content-Security-Policy": page_hosting.PAGE_CSP,
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

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

    def _actor(request: Request) -> str:
        """操作人：A 期没有登录，先用请求头；缺省 anonymous 便于审计区分。"""
        return request.headers.get("X-User-Id") or "anonymous"

    def _require_ops(request: Request, action: str, run_id: str | None = None) -> None:
        """写操作鉴权：失败时先落 denied 审计再抛 401——越权尝试也必须可查。"""
        if _ops_allowed(request):
            return
        audit.record(
            action=action,
            object_type="run",
            object_id=run_id,
            actor=_actor(request),
            result="denied",
            detail="运维令牌不正确或未配置",
        )
        raise HTTPException(status_code=401, detail="运维令牌不正确或未配置")

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(request: Request, run_id: str) -> dict[str, Any]:
        """人工停止：只置标志，节点边界生效；queued 的立即取消。"""
        _require_ops(request, "run.cancel", run_id)
        outcome = run_state.request_cancel(run_id, actor=_actor(request))
        if outcome == "missing":
            raise HTTPException(status_code=404, detail="run 不存在")
        audit.record(
            action="run.cancel",
            object_type="run",
            object_id=run_id,
            actor=_actor(request),
            detail=outcome,
        )
        return {"ok": True, "result": outcome}

    @app.post("/api/runs/{run_id}/retry")
    def retry_run(request: Request, run_id: str) -> dict[str, Any]:
        """人工重试：failed/cancelled 回队列（epoch+1）；succeeded 需新建 run。"""
        _require_ops(request, "run.retry", run_id)
        if not run_state.requeue(run_id, actor=_actor(request)):
            row = fetch_one("SELECT status FROM runs WHERE id = %s", (run_id,))
            if row is None:
                raise HTTPException(status_code=404, detail="run 不存在")
            audit.record(
                action="run.retry",
                object_type="run",
                object_id=run_id,
                actor=_actor(request),
                result="failed",
                detail=f"当前状态不可重试: {row['status']}",
            )
            raise HTTPException(status_code=409, detail=f"当前状态不可重试: {row['status']}")
        audit.record(
            action="run.retry", object_type="run", object_id=run_id, actor=_actor(request)
        )
        return {"ok": True, "status": "queued"}

    @app.get("/api/audit")
    def read_audit(
        request: Request,
        actor: str | None = None,
        object_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """审计查询：运维留痕（operator_actions）+ run 事件（run_events）两侧都能查。"""
        _require_ops(request, "audit.read", object_id)
        try:
            since_at = _parse_instant(since) if since else None
            until_at = _parse_instant(until) if until else None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        actions = audit.query(
            actor=actor,
            object_id=object_id,
            since=since_at,
            until=until_at,
            limit=limit,
        )
        events: list[dict[str, Any]] = []
        if object_id:
            # 对象 id 就是 run id：把 run 事件一并带出来，省得再拼一次请求
            events = [
                dict(row)
                for row in fetch_all(
                    "SELECT seq, run_id, type, payload, actor, created_at FROM run_events"
                    " WHERE run_id = %s ORDER BY created_at DESC LIMIT %s",
                    (object_id, max(1, min(500, limit))),
                )
            ]
        return {"ok": True, "actions": actions, "events": events}

    def _tenant(request: Request) -> str:
        """租户：A 期同样先用请求头，缺省落到默认租户（与路由层现有约定一致）。"""
        return request.headers.get("X-Tenant-Id") or settings.default_tenant

    @app.get("/p/{workflow_id}/{version}/{page_id}")
    def page_shell(
        request: Request, workflow_id: str, version: int, page_id: str
    ) -> HTMLResponse:
        """页面壳：渲染 iframe（只给 allow-scripts，不给 allow-same-origin）。"""
        try:
            root = page_hosting.package_root(workflow_id, version)
            page_hosting.page_dir(root, page_id)
        except page_hosting.HostingError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        shell = page_hosting.shell_html(
            workflow_id=workflow_id,
            version=version,
            page_id=page_id,
            user_id=_user(request),
            tenant_id=_tenant(request),
        )
        return HTMLResponse(
            shell,
            headers={
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/p/{workflow_id}/{version}/{page_id}/{asset:path}")
    def page_asset(
        workflow_id: str, version: int, page_id: str, asset: str
    ) -> FileResponse:
        """托管编译产物：只放行 index.html / app.js / theme.css / assets/**。"""
        try:
            root = page_hosting.package_root(workflow_id, version)
            path = page_hosting.resolve_asset(root, page_id, asset)
        except page_hosting.HostingError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            path,
            headers={
                "Content-Security-Policy": page_hosting.PAGE_CSP,
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                # 版本不可变：静态资源可长缓存；入口页带 token，禁缓存
                "Cache-Control": (
                    "no-store" if asset == "index.html" else "public, max-age=86400"
                ),
            },
        )

    def _page_context(
        request: Request, workflow_id: str, version: int, page_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """校验 token 并加载页面 Schema；token 与页面/版本任一不匹配都按 401。"""
        try:
            payload = page_hosting.verify_token(request.query_params.get("token", ""))
        except page_hosting.HostingError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if (
            payload.get("w") != workflow_id
            or int(payload.get("v") or -1) != int(version)
            or payload.get("p") != page_id
        ):
            raise HTTPException(status_code=401, detail="token 与页面不匹配")
        try:
            root = page_hosting.package_root(workflow_id, version)
            schema = page_hosting.page_schema(root, page_id)
        except page_hosting.HostingError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return payload, schema

    def _read_page_data(
        payload: dict[str, Any], schema: dict[str, Any], name: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """按 Schema 声明的数据源名取数；声明里没有的键一律 400。"""
        declared = (schema.get("data") or {}).get(name)
        if not isinstance(declared, dict):
            raise HTTPException(status_code=400, detail=f"未声明的数据源: {name}")
        # Schema 里的静态参数（如 mcp.read 的 server/tool）先垫底，查询串里的
        # 运行期取值（$selection 等在前端解析）覆盖同名键
        merged: dict[str, Any] = {
            str(key): value
            for key, value in (declared.get("params") or {}).items()
            if not (isinstance(value, str) and value.startswith("$"))
        }
        merged.update(params)
        try:
            rows = page_data.dispatch(
                declared["source"], tenant_id=str(payload.get("t") or ""), params=merged
            )
        except page_data.DataSourceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 单对象数据源查不到就是"不存在或无权访问"，不给 200 空 body 让前端误判
        if not rows and declared["source"] in {"run.get", "run.events", "artifact.read"}:
            raise HTTPException(status_code=404, detail="对象不存在或无权访问")
        return {"ok": True, "rows": rows}

    @app.get("/api/page/{workflow_id}/{version}/{page_id}/data")
    def page_data_endpoint(
        request: Request, workflow_id: str, version: int, page_id: str
    ) -> dict[str, Any]:
        """兼容入口：`?source=<数据源名>`。新客户端走 `/{name}` 路径。"""
        payload, schema = _page_context(request, workflow_id, version, page_id)
        name = request.query_params.get("source") or ""
        params = {
            key: value
            for key, value in request.query_params.items()
            if key not in {"token", "source"}
        }
        return _read_page_data(payload, schema, name, params)

    @app.get("/api/page/{workflow_id}/{version}/{page_id}/{name}")
    def page_data_named(
        request: Request, workflow_id: str, version: int, page_id: str, name: str
    ) -> dict[str, Any]:
        """编译产物实际使用的形态：数据源名直接作为路径段。"""
        payload, schema = _page_context(request, workflow_id, version, page_id)
        params = {
            key: value
            for key, value in request.query_params.items()
            if key != "token"
        }
        return _read_page_data(payload, schema, name, params)

    @app.post("/api/page/{workflow_id}/{version}/{page_id}/action")
    async def page_action(
        request: Request, workflow_id: str, version: int, page_id: str
    ) -> dict[str, Any]:
        """页面写动作：只认白名单里的 op，且 run 级动作必须落在同租户。

        `node.invoke` 需要执行器支持"单节点调用"这个载体（目前没有），
        先明确 400，避免前端以为按钮点了没反应。
        """
        payload, _ = _page_context(request, workflow_id, version, page_id)
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001 — 非法 JSON 统一 400
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")

        op = body.get("action")
        if op not in PAGE_ACTIONS:
            raise HTTPException(status_code=400, detail=f"动作不在白名单: {op!r}")
        tenant_id = str(payload.get("t") or "")
        user_id = str(payload.get("u") or "")
        actor = f"page:{user_id}"
        scope = f"{workflow_id}@{version}:{page_id}"

        if op == "node.invoke":
            raise HTTPException(
                status_code=400, detail="node.invoke 需要执行器的单节点载体，尚未实现"
            )

        run_id = str(body.get("run") or "")
        row = None
        if run_id:
            row = fetch_one(
                "SELECT id, status FROM runs WHERE id = %s AND tenant_id = %s",
                (run_id, tenant_id),
            )
            if row is None:
                audit.record(
                    action=f"page.{op}",
                    object_type="run",
                    object_id=run_id,
                    actor=actor,
                    scope=scope,
                    result="denied",
                    detail="run 不存在或跨租户",
                )
                raise HTTPException(status_code=404, detail="对象不存在或无权访问")

        if op in {"run.cancel", "run.retry"}:
            if row is None:
                raise HTTPException(status_code=400, detail=f"{op} 需要 run 参数")
            if op == "run.cancel":
                outcome = run_state.request_cancel(run_id, actor=actor)
                ok = outcome in {"cancelled", "cancelling"}
            else:
                ok = run_state.requeue(run_id, actor=actor)
                outcome = "queued" if ok else "not_retryable"
        else:  # run.trigger
            version_row = fetch_one(
                "SELECT package_ref FROM workflow_versions"
                " WHERE workflow_id = %s AND version = %s",
                (workflow_id, version),
            )
            if version_row is None:
                raise HTTPException(status_code=404, detail="版本不存在")
            run_id, created = runs_store.create_run(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                version=version,
                package_ref=str(version_row["package_ref"]),
                subject={"user_id": user_id, "payload": body.get("payload") or {}},
                window=str((body.get("payload") or {}).get("window") or f"page-{page_id}"),
                trigger_user_id=user_id,
            )
            outcome = "created" if created else "deduped"
            ok = True

        audit.record(
            action=f"page.{op}",
            object_type="run",
            object_id=run_id or None,
            actor=actor,
            scope=scope,
            result="ok" if ok else "failed",
            detail=f"status={outcome}",
        )
        return {"ok": ok, "status": outcome, "run_id": run_id or None}

    def _share(token: str) -> dict[str, Any]:
        """解析分享 token；不可用统一 404，次数用尽 429（要能区分"过载"与"不存在"）。"""
        try:
            return share_store.resolve(token)
        except share_store.ShareError as exc:
            status = 429 if exc.code == "exhausted" else 404
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.get("/s/{token}")
    def share_shell(request: Request, token: str) -> HTMLResponse:
        """分享页壳：不需要登录，iframe 沙箱与登录态一致。"""
        share = _share(token)
        try:
            root = page_hosting.package_root(share["workflow_id"], int(share["version"]))
            page_hosting.page_dir(root, share["page_id"])
        except page_hosting.HostingError as exc:
            share_store.touch(share, status=404)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        share_store.touch(share, status=200)
        return HTMLResponse(
            page_hosting.share_shell_html(token=token, page_id=str(share["page_id"])),
            headers={
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/s/{token}/data/{name}")
    def share_data(request: Request, token: str, name: str) -> dict[str, Any]:
        """分享态取数：scope 进 SQL 谓词，单对象再核一次范围，越界按不存在。"""
        share = _share(token)
        try:
            root = page_hosting.package_root(share["workflow_id"], int(share["version"]))
            schema = page_hosting.page_schema(root, str(share["page_id"]))
        except page_hosting.HostingError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        declared = (schema.get("data") or {}).get(name)
        if not isinstance(declared, dict):
            share_store.touch(share, status=400)
            raise HTTPException(status_code=400, detail=f"未声明的数据源: {name}")

        scope = dict(share.get("scope") or {})
        params = {
            key: value for key, value in request.query_params.items() if key != "token"
        }
        params.setdefault("workflow_id", str(share["workflow_id"]))
        params.update(
            {
                key: value
                for key, value in (declared.get("params") or {}).items()
                if not (isinstance(value, str) and value.startswith("$"))
            }
        )
        try:
            rows = page_data.dispatch(
                declared["source"],
                tenant_id=str(share["tenant_id"]),
                params=params,
                scope=scope,
            )
        except page_data.DataSourceError as exc:
            share_store.touch(share, status=400)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if declared["source"] in {"run.get", "run.events", "artifact.read"}:
            rows = [
                row
                for row in rows
                if share_store.row_in_scope(
                    scope, row, workflow_id=str(share["workflow_id"])
                )
            ]
        if not rows and declared["source"] in {"run.get", "run.events", "artifact.read"}:
            share_store.touch(share, status=404, in_scope=False)
            raise HTTPException(status_code=404, detail="对象不存在或不在分享范围")
        share_store.touch(share, status=200)
        return {"ok": True, "rows": rows}

    # 放在 /s/{token}/data/{name} 之后：catch-all 先声明会把 data 路径吃掉
    _SHARE_MEDIA = {
        "index.html": "text/html; charset=utf-8",
        "app.js": "application/javascript; charset=utf-8",
        "theme.css": "text/css; charset=utf-8",
    }

    @app.get("/s/{token}/{asset:path}")
    def share_asset(token: str, asset: str) -> Response:
        share = _share(token)
        try:
            root = page_hosting.package_root(share["workflow_id"], int(share["version"]))
            page_hosting.page_dir(root, str(share["page_id"]))
        except page_hosting.HostingError as exc:
            share_store.touch(share, status=404)
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if asset in _SHARE_MEDIA:
            # 分享态现投影再编译：去写操作与成本字段，发布物本身不动
            schema = page_hosting.page_schema(root, str(share["page_id"]))
            try:
                files = page_compiler.compile_page(page_share_view.project(schema))
            except (page_schema.PageSchemaError, AssertionError) as exc:
                share_store.touch(share, status=404)
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            share_store.touch(share, status=200)
            return Response(
                files[asset],
                media_type=_SHARE_MEDIA[asset],
                headers={
                    "Content-Security-Policy": page_hosting.PAGE_CSP,
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )

        try:
            path = page_hosting.resolve_asset(root, str(share["page_id"]), asset)
        except page_hosting.HostingError as exc:
            share_store.touch(share, status=404)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            path,
            headers={
                "Content-Security-Policy": page_hosting.PAGE_CSP,
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": (
                    "no-store" if asset == "index.html" else "public, max-age=86400"
                ),
            },
        )

    @app.post("/s/{token}/action")
    def share_action(token: str) -> dict[str, Any]:
        """分享态一律不许写：免登录链接不能成为改数据的入口。"""
        share = _share(token)
        share_store.touch(share, status=403)
        audit.record(
            action="share.action",
            object_type="share",
            object_id=str(share["id"]),
            actor="share:anonymous",
            scope="share",
            result="denied",
            detail="分享态不允许执行动作",
        )
        raise HTTPException(status_code=403, detail="分享页面为只读，不能执行动作")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "mock": settings.mock, "env": settings.env}

    return app


app = create_app()
