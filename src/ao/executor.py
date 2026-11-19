"""run 执行器：加载冻结包 → 构建 LangGraph → 逐节点执行 → 记账/事件/终态。

不变量：
- 一个节点 = 一次 harness turn（repair 是同一 attempt 内的第二次 turn）；
- 图状态只管"走到哪"，DSH 会话只管"说到哪句"，两者在 `run_steps.native_id` 对齐；
- 所有写库都带 `epoch` 条件（fencing），旧进程写不进去；
- 停止只在节点边界生效。
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any, TypedDict

from psycopg.types.json import Jsonb

from ao import contracts, events, memory, resources, usage
from ao import state as state_machine
from ao.db import fetch_one, read, tx
from ao.gateway import gateway_root
from ao.harness.base import Provider, Reply
from ao.harness.mock import MockProvider
from ao.input_guard import InputGuardError, sanitize_inputs
from ao.manifest import Binding, Manifest, Node, eval_when, parse
from ao.package import Package, load_package
from ao.settings import settings
from wf_sdk.runtime import (
    NodeContext,
    RunEnv,
    current_run_env,
    reset_context,
    reset_run_env,
    set_context,
    set_run_env,
)

logger = logging.getLogger(__name__)

HOST = socket.gethostname()


class RunCancelled(RuntimeError):
    """用户请求停止，在节点边界抛出。"""


class FencedError(RuntimeError):
    """epoch 不匹配：本进程已不是该 run 的合法 owner。"""


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status: str = "fail", meta: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.meta = meta or {}


class GraphState(TypedDict, total=False):
    run_id: str
    attempt: int
    vars: dict[str, Any]
    nodes: dict[str, dict[str, Any]]
    subject: dict[str, Any]
    last: str


def current_provider() -> Provider:
    """不静默降级：真跑模式下 SDK 缺失就在启动时报错。"""
    if settings.mock:
        return MockProvider()
    from ao.harness.dsh import DshProvider, sdk_available

    if not sdk_available():
        raise RuntimeError("AO_MOCK=0 但 deepseek-harness-sdk 未安装：uv sync --extra harness")
    return DshProvider()


# --- 运行期回调：SDK 的 agent()/emit() 只通过这里碰平台 -------------------------


class RunRuntime:
    def __init__(
        self,
        *,
        run: dict[str, Any],
        manifest: Manifest,
        package: Package,
        provider: Provider,
    ) -> None:
        self.run_id = str(run["id"])
        self.epoch = int(run["epoch"])
        self.tenant = str(run["tenant_id"])
        self.manifest = manifest
        self.package = package
        self.provider = provider
        self.node_cost = 0.0
        self.repair_count = 0
        self.native_id: str | None = None
        self.current_node: Node | None = None
        self.budget_usd = float(run.get("budget_usd") or 0)
        self.spent_usd = float(run.get("spent_usd") or 0)
        self.user_id = run.get("trigger_user_id")
        # 节点级工具集缓存：key 是节点 id，value 是 (mcp_servers, skill_refs)。
        # 一个节点可能跑多个 turn（重试 / repair），解析一次即可；不同节点互不影响。
        self._tool_cache: dict[str, tuple[dict[str, Any], list[str]]] = {}
        # 工作流级默认工具集：作为 RunEnv / ctx.mcp 的兜底视图，真正注入 harness 的工具集
        # 由 _tools_for() 按节点解析。发布期已校验，这里再解析是兜底（资源可能发布后被下架）
        self.mcp_servers, self.skill_refs, skipped = resources.resolve_bindings(
            manifest.effective_bindings(), user_id=self.user_id
        )
        if skipped:
            logger.warning("绑定不可用，已跳过: %s", skipped)

    # --- fencing / cancel ---
    def _assert_owner(self, conn: Any) -> dict[str, Any]:
        row = conn.execute(
            "SELECT epoch, cancel_requested, status FROM runs WHERE id = %s", (self.run_id,)
        ).fetchone()
        if row is None:
            raise FencedError("run 不存在")
        if int(row["epoch"]) != self.epoch:
            raise FencedError(f"epoch 不匹配: 本地 {self.epoch}，库中 {row['epoch']}")
        if row["cancel_requested"] or row["status"] == "cancelled":
            raise RunCancelled("用户请求停止")
        return row

    def check_cancel(self) -> None:
        with read() as conn:
            self._assert_owner(conn)

    # --- SDK 回调 ---
    # --- 变量解析：run 级一次、节点级懒解析、值落 run_vars 复用 ---
    def _cached_vars(self, node_id: str) -> dict[str, Any]:
        from ao.db import fetch_all

        rows = fetch_all(
            "SELECT name, value FROM run_vars WHERE run_id = %s AND node_id = %s",
            (self.run_id, node_id),
        )
        return {str(row["name"]): row["value"] for row in rows}

    def _persist_var(self, node_id: str) -> Any:
        def put(name: str, value: Any, source: str) -> None:
            with tx() as conn:
                conn.execute(
                    "INSERT INTO run_vars (run_id, node_id, name, value, source)"
                    " VALUES (%s,%s,%s,%s,%s)"
                    " ON CONFLICT (run_id, node_id, name) DO UPDATE"
                    " SET value = EXCLUDED.value, source = EXCLUDED.source, resolved_at = now()",
                    (self.run_id, node_id, name, Jsonb(value), source),
                )

        return put

    def _resolve(
        self, specs: dict[str, Any], base: dict[str, Any], node_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        from ao.vars import VarError, resolve_specs

        if not specs:
            return {}, []
        try:
            return resolve_specs(
                specs,
                base,
                cached=self._cached_vars(node_id),
                persist=self._persist_var(node_id),
                backoff=settings.var_backoff_seconds,
            )
        except VarError as exc:
            # 取数失败必须留痕：否则线上只能看到节点失败，看不到是哪个 MCP 挂了
            with tx() as conn:
                events.emit(
                    conn,
                    self.run_id,
                    "var.failed",
                    node_id=node_id or None,
                    payload={
                        "var": exc.var,
                        "tool": exc.tool,
                        "kind": exc.kind,
                        "error": str(exc),
                        "ok": False,
                    },
                )
            raise

    def resolve_run_vars(self, base: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return self._resolve(self.manifest.vars.get("run") or {}, base, "")

    def resolve_node_vars(
        self, node_id: str, base: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        from ao.manifest import variables_of

        return self._resolve(variables_of(self.manifest, node_id), base, node_id)

    def emit_var_calls(self, node_id: str | None, calls: list[dict[str, Any]]) -> None:
        if not calls:
            return
        with tx() as conn:
            for call in calls:
                events.emit(
                    conn,
                    self.run_id,
                    "var.resolved" if call.get("ok") else "var.failed",
                    node_id=node_id,
                    payload=call,
                )
                if call.get("ok") and not call.get("cached"):
                    events.emit(
                        conn,
                        self.run_id,
                        "tool.call",
                        node_id=node_id,
                        payload={"tool": call.get("tool"), "var": call.get("var")},
                    )

    def node_started(self, ctx: Any, spec: Any) -> None:
        from ao.gateway import BudgetExceeded

        # 软预算：节点开始前查一次，耗尽就暂停而不是继续烧
        if self.budget_usd > 0 and self.spent_usd >= self.budget_usd:
            raise BudgetExceeded(402, f"预算耗尽: {self.spent_usd:.6f}/{self.budget_usd:.6f}")
        self.repair_count = 0
        self.node_cost = 0.0
        self.native_id = None
        node = self.manifest.node(spec.id)
        self.current_node = node
        # ctx.mcp / ctx.skills 是 SDK 侧的只读视图：必须与真正注入 harness 的工具集一致，
        # 否则 DSL 读 ctx.mcp 会看到工作流级默认值，与节点级绑定的实际行为不符
        servers, skill_refs = self._tools_for(node)
        ctx.mcp = sorted(servers)
        ctx.skills = list(skill_refs)
        # 同机接管才 resume；跨机 session 文件不在本地，重跑当前节点
        resume_native = None
        with read() as conn:
            row = conn.execute(
                "SELECT native_id, host FROM run_steps"
                " WHERE run_id = %s AND node_id = %s AND attempt = %s"
                " ORDER BY step DESC LIMIT 1",
                (self.run_id, spec.id, ctx.attempt),
            ).fetchone()
            if row and row["host"] == HOST and row["native_id"]:
                resume_native = row["native_id"]
        self.native_id = resume_native

        with tx() as conn:
            self._assert_owner(conn)
            conn.execute(
                "INSERT INTO run_nodes (run_id, node_id, attempt, kind, status, started_at, phase)"
                " VALUES (%s,%s,%s,%s,'running',now(),'prompt')"
                " ON CONFLICT (run_id, node_id, attempt) DO UPDATE"
                " SET status='running', started_at=now(), ended_at=NULL,"
                " error=NULL, phase='prompt'",
                (self.run_id, spec.id, ctx.attempt, spec.kind),
            )
            updated = conn.execute(
                "UPDATE runs SET current_node = %s, status = 'running'"
                " WHERE id = %s AND epoch = %s",
                (spec.id, self.run_id, self.epoch),
            )
            if updated.rowcount == 0:
                raise FencedError("节点开始写库被 fencing 拒绝")
            events.emit(
                conn,
                self.run_id,
                "node.started",
                node_id=spec.id,
                payload={"attempt": ctx.attempt, "kind": spec.kind, "resume": bool(resume_native)},
            )

        # 节点级变量懒解析：失败抛 missing_var，由外层把节点判失败（不静默降级）
        values, calls = self.resolve_node_vars(
            spec.id, {"subject": ctx.subject, "vars": ctx.vars, "nodes": ctx.nodes}
        )
        ctx.vars.update(values)
        self.emit_var_calls(spec.id, calls)

    def node_finished(self, ctx: Any, spec: Any, data: dict[str, Any]) -> None:
        with tx() as conn:
            self._assert_owner(conn)
            conn.execute(
                "UPDATE run_nodes SET status='succeeded', output=%s, cost=%s, native_id=%s,"
                " repair_count=%s, phase='done', ended_at=now(), meta=%s"
                " WHERE run_id=%s AND node_id=%s AND attempt=%s",
                (
                    Jsonb(data),
                    self.node_cost,
                    self.native_id,
                    self.repair_count,
                    Jsonb({"system_injected": True}),
                    self.run_id,
                    spec.id,
                    ctx.attempt,
                ),
            )
            usage.refresh_run_spend(conn, self.run_id)
            events.emit(
                conn,
                self.run_id,
                "node.finished",
                node_id=spec.id,
                payload={
                    "cost": round(self.node_cost, 6),
                    "repair": self.repair_count,
                    "native_id": self.native_id,
                },
            )

    def node_failed(self, ctx: Any, spec: Any, exc: Exception) -> None:
        code = classify(exc)
        with tx() as conn:
            conn.execute(
                "UPDATE run_nodes SET status='failed', error=%s, cost=%s, native_id=%s,"
                " repair_count=%s, ended_at=now()"
                " WHERE run_id=%s AND node_id=%s AND attempt=%s",
                (
                    f"{code}: {exc}",
                    self.node_cost,
                    self.native_id,
                    self.repair_count,
                    self.run_id,
                    spec.id,
                    ctx.attempt,
                ),
            )
            events.emit(
                conn,
                self.run_id,
                "node.failed",
                node_id=spec.id,
                payload={"code": code, "error": str(exc)[:500], "attempt": ctx.attempt},
            )

    def emit(self, ctx: Any, event: str, payload: dict[str, Any]) -> None:
        with tx() as conn:
            events.emit(conn, self.run_id, event, node_id=ctx.node_id, payload=payload)

    def _emit_run_event(
        self, event: str, *, node_id: str | None, payload: dict[str, Any]
    ) -> None:
        """落一条 run 事件；失败只记日志——留痕不能反过来把节点搞挂。"""
        try:
            with tx() as conn:
                events.emit(conn, self.run_id, event, node_id=node_id, payload=payload)
        except Exception:  # noqa: BLE001 — 事件写不进去不影响执行结果
            logger.debug("事件落库失败: %s node=%s", event, node_id, exc_info=True)

    def _tools_for(self, node: Node | None) -> tuple[dict[str, Any], list[str]]:
        """节点实际可用的 (mcp_servers, skill_refs)，按节点缓存。

        解析来源是 `manifest.bindings_for_node()`（工作流级默认 + 节点级覆盖），
        不是全 run 一次的 `effective_bindings()`——后者会让所有节点共用一套工具。
        """
        if node is None:
            return dict(self.mcp_servers), list(self.skill_refs)
        cached = self._tool_cache.get(node.id)
        if cached is not None:
            return cached
        servers, skills, skipped = resources.resolve_bindings(
            self.manifest.bindings_for_node(node), user_id=self.user_id
        )
        if skipped:
            # 必须带 node_id：否则线上只能看到"某个绑定不可用"，定位不到哪个节点
            for item in skipped:
                item["node"] = node.id
            logger.warning("节点 %s 的绑定不可用，已跳过: %s", node.id, skipped)
            self._emit_run_event(
                "binding.skipped", node_id=node.id, payload={"items": skipped}
            )
        self._tool_cache[node.id] = (servers, skills)
        # 每节点一条：这是"这个节点到底用了哪些工具"的唯一可审计来源
        self._emit_run_event(
            "binding.resolved",
            node_id=node.id,
            payload={"mcp": sorted(servers), "skills": skills},
        )
        return servers, skills

    def _tools_for_turn(
        self, ctx: Any, mcp: list[str] | None, skills: list[str] | None
    ) -> tuple[dict[str, Any], list[str]]:
        """本 turn 的工具集：节点绑定为底，DSL 显式传参作为该 turn 的临时覆盖。

        `agent(mcp=[...])` 以前被 `call_agent` 丢弃，这里才真正生效；
        覆盖只作用于当前 turn，不写缓存，也不影响同 run 其他节点。
        """
        servers, skill_refs = self._tools_for(self.manifest.node(ctx.node_id))
        if mcp is None and skills is None:
            return servers, skill_refs
        skipped: list[dict[str, Any]] = []
        if mcp is not None:
            servers, _, lost = resources.resolve_bindings(
                [Binding(kind="mcp", ref=str(ref)) for ref in mcp], user_id=self.user_id
            )
            skipped.extend(lost)
        if skills is not None:
            _, skill_refs, lost = resources.resolve_bindings(
                [Binding(kind="skill", ref=str(ref)) for ref in skills], user_id=self.user_id
            )
            skipped.extend(lost)
        if skipped:
            self._emit_run_event(
                "binding.skipped",
                node_id=ctx.node_id,
                payload={"items": skipped, "source": "dsl"},
            )
        self._emit_run_event(
            "binding.override",
            node_id=ctx.node_id,
            payload={"mcp": mcp, "skills": skills},
        )
        return servers, skill_refs

    async def call_agent(
        self,
        ctx: Any,
        *,
        prompt: str,
        vars: dict[str, Any],  # noqa: A002 — 与生成代码参数名保持一致
        mcp: list[str] | None = None,
        skills: list[str] | None = None,
    ) -> str:
        return await self._turn(ctx, prompt=prompt, repair=False, mcp=mcp, skills=skills)

    async def _turn(
        self,
        ctx: Any,
        *,
        prompt: str,
        repair: bool,
        mcp: list[str] | None = None,
        skills: list[str] | None = None,
    ) -> str:
        node = self.manifest.node(ctx.node_id)
        timeout = float(node.timeout if node else 600)
        # 本 turn 的工具集：节点绑定为底，DSL 显式传参临时覆盖（P6-T02/T03）
        servers, skill_refs = self._tools_for_turn(ctx, mcp, skills)
        # harness 的 cwd 就是包根，会在那里自动加载 AGENTS.md / CLAUDE.md 等文件。
        # 包由模型生成，若把这类文件留在包根，下一轮 turn 会被当作指令读入（自我注入），
        # 因此每次调用 harness 前先清一遍；删不掉按 fail closed 处理。
        sanitize_inputs(self.package.root)
        # system 序言由 provider 拼接，且必须留痕（见设计 §7.2）
        system = ctx.system()
        # 网关模式：本 turn 签一个虚拟 key。预算由网关按 key 硬限（超了 402），
        # 账单靠 SpendLogs.api_key → gateway_keys 反查回归因；turn 结束即吊销。
        virtual = await self._open_gateway_key(ctx, node)
        try:
            reply: Reply = await self.provider.send(
                run_id=self.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                system=system,
                prompt=prompt,
                contract=contracts.contract_of(
                    {k: v.__name__ for k, v in _outputs_of(node).items()}
                ),
                repair=repair,
                native_id=self.native_id,
                context={
                    "capability": getattr(node, "capability", "read_only"),
                    "cwd": str(self.package.root),
                    "timeout": timeout,
                    # 本节点（或本 turn 的 DSL 覆盖）工具集：provider 侧拼进 harness 的 MCP 补丁
                    "mcp_servers": servers,
                    "skills": skill_refs,
                    # provider 用这两个覆盖全局 base_url/api_key（None 表示直连）
                    "api_key": virtual.key if virtual else None,
                    "gateway_base_url": gateway_root() if virtual else None,
                },
                on_activity=lambda event: self._on_activity(ctx, event),
            )
        finally:
            if virtual is not None:
                await self._close_gateway_key(virtual)
        if reply.native_id:
            self.native_id = reply.native_id
            self._save_step(ctx, reply.native_id)
        with tx() as conn:
            self._assert_owner(conn)
            if reply.usage:
                cost = usage.record(
                    conn,
                    run_id=self.run_id,
                    node_id=ctx.node_id,
                    attempt=ctx.attempt,
                    usage=reply.usage,
                    user_id=self.user_id,
                )
                self.node_cost += cost
                self.spent_usd = usage.refresh_run_spend(conn, self.run_id)
        if reply.status in {"budget", "budget_exceeded"}:
            from ao.gateway import BudgetExceeded

            raise BudgetExceeded(402, "provider 报预算耗尽")
        if reply.status != "ok":
            raise ProviderError(
                f"provider 返回 {reply.status}", status=reply.status, meta=reply.meta
            )
        return reply.text

    async def _open_gateway_key(self, ctx: Any, node: Any) -> Any:
        """签发本 turn 的虚拟 key；未启用网关或 mock 模式返回 None（直连上游）。"""
        if not settings.gateway_enabled or settings.mock:
            return None
        from ao import gateway as gw

        # spend 落库滞后 2-4s，签发前刷新一次，让 max_budget 尽量贴近真实剩余
        with tx() as conn:
            self.spent_usd = usage.refresh_run_spend(conn, self.run_id)
        remaining = self.budget_usd - self.spent_usd if self.budget_usd > 0 else 0.0
        if self.budget_usd > 0 and remaining <= 0:
            raise gw.BudgetExceeded(
                402, f"预算耗尽: {self.spent_usd:.6f}/{self.budget_usd:.6f}"
            )
        try:
            virtual = await gw.current_gateway().issue_key(
                run_id=self.run_id,
                node_id=ctx.node_id,
                attempt=ctx.attempt,
                purpose="node",
                budget_usd=max(remaining, 0.0),
            )
        except gw.BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 — 网关故障按配置决定降级或失败
            if not settings.gateway_fail_open:
                raise
            logger.warning("网关不可用，按 fail-open 直连上游: %s", exc)
            self.emit(ctx, "gateway.degraded", {"error": str(exc)[:300]})
            return None
        gw.bind_key(
            virtual,
            run_id=self.run_id,
            node_id=ctx.node_id,
            attempt=ctx.attempt,
            purpose="node",
            budget_usd=max(remaining, 0.0),
        )
        return virtual

    async def _close_gateway_key(self, key: Any) -> None:
        from ao import gateway as gw

        try:
            await gw.current_gateway().revoke_key(key.key)
        except Exception:  # noqa: BLE001 — 吊销失败不能拖垮 turn，支出仍以流水为准
            logger.warning("虚拟 key 吊销失败", exc_info=True)
        try:
            gw.mark_revoked(key.key_id)
        except Exception:  # noqa: BLE001
            logger.warning("虚拟 key 登记表回写失败", exc_info=True)

    def _on_activity(self, ctx: Any, event: dict[str, Any]) -> None:
        tool = event.get("tool") or event.get("name")
        if not tool:
            return
        try:
            with tx() as conn:
                events.emit(
                    conn,
                    self.run_id,
                    "tool.call",
                    node_id=ctx.node_id,
                    payload={"tool": tool, "ok": event.get("ok", True)},
                )
        except Exception:  # noqa: BLE001 — 进度通知失败不影响节点
            logger.debug("tool.call 落库失败", exc_info=True)

    def _save_step(self, ctx: Any, native_id: str) -> None:
        with tx() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(step), -1) + 1 AS next FROM run_steps"
                " WHERE run_id = %s AND node_id = %s AND attempt = %s",
                (self.run_id, ctx.node_id, ctx.attempt),
            ).fetchone()
            conn.execute(
                "INSERT INTO run_steps (run_id, node_id, attempt, step, native_id, host, phase)"
                " VALUES (%s,%s,%s,%s,%s,%s,'prompt')",
                (self.run_id, ctx.node_id, ctx.attempt, int(row["next"]), native_id, HOST),
            )

    async def run_stage(self, ctx: Any, node: Node) -> dict[str, Any]:
        """一个阶段 = 一个新 harness 会话 + 会话内多轮。

        会话边界：`_turn` 里的 `native_id` 只在本阶段内递增（repair 续轮），
        节点结束就丢弃；下一节点从 `node_started` 重新取值，跨阶段不复用。

        多轮规则：首轮用节点 prompt；产物不合格就在**同一会话**里追加一轮反馈，
        直到满足契约或达到 `stage_max_turns`（默认 3，含首轮）。
        """
        outputs = {name: kind.__name__ for name, kind in _outputs_of(node).items()}
        # 跨阶段交接靠文件：开场把前序摘要、上游产物与已产出文件拼进首轮 prompt
        block = memory.bootstrap_block(self.package.root, node.id, ctx.nodes)
        prompt = f"{block}\n\n---\n\n{ctx.prompt()}" if block else ctx.prompt()
        text = await self._turn(ctx, prompt=prompt, repair=False)
        data, errors = contracts.validate(text, outputs)
        max_turns = max(1, int(getattr(settings, "stage_max_turns", 3)))
        turn = 1
        while errors and turn < max_turns:
            turn += 1
            self.repair_count += 1
            with tx() as conn:
                events.emit(
                    conn,
                    self.run_id,
                    "node.repair",
                    node_id=ctx.node_id,
                    payload={"errors": errors, "turn": turn, "max_turns": max_turns},
                )
            text = await self._turn(
                ctx, prompt=contracts.repair_prompt(errors, outputs), repair=True
            )
            data, errors = contracts.validate(text, outputs)
        if errors:
            raise contracts_error(errors)
        return data or {}

    async def summarize_stage(self, ctx: Any, node: Node, data: dict[str, Any]) -> None:
        """阶段收尾摘要：起一个独立 read_only 会话，把结论追加进 `memory.md`。

        摘要失败不阻断 run：下游只是少一段记忆，不能让已经成功的节点判失败。
        mock 模式跳过——mock 回复没有语义，写进去只会污染后续阶段的上下文。
        """
        if settings.mock or not getattr(settings, "stage_summary_enabled", True):
            return
        try:
            reply: Reply = await self.provider.send(
                run_id=self.run_id,
                node_id=f"{node.id}#summary",
                attempt=ctx.attempt,
                system="你是阶段收尾的记录员，只输出摘要正文。",
                prompt=memory.summary_prompt(node.id, data),
                contract=None,
                repair=False,
                native_id=None,
                context={
                    "capability": "read_only",
                    "cwd": str(self.package.root),
                    "timeout": 120.0,
                },
            )
        except Exception:  # noqa: BLE001 — 摘要失败不影响主链路
            logger.warning("阶段摘要失败: run=%s node=%s", self.run_id, node.id, exc_info=True)
            return
        if reply.status != "ok" or not (reply.text or "").strip():
            logger.warning(
                "阶段摘要未产出: run=%s node=%s status=%s", self.run_id, node.id, reply.status
            )
            return
        memory.append_stage_summary(self.package.root, node.id, reply.text)
        if reply.usage:
            with tx() as conn:
                cost = usage.record(
                    conn,
                    run_id=self.run_id,
                    node_id=node.id,
                    attempt=ctx.attempt,
                    usage=reply.usage,
                    purpose="summary",
                    user_id=self.user_id,
                )
                conn.execute(
                    "UPDATE run_nodes SET cost = cost + %s"
                    " WHERE run_id = %s AND node_id = %s AND attempt = %s",
                    (cost, self.run_id, node.id, ctx.attempt),
                )
                usage.refresh_run_spend(conn, self.run_id)


def contracts_error(errors: list[str]) -> Exception:
    from wf_sdk import ContractError

    return ContractError(errors)


def _outputs_of(node: Node | None) -> dict[str, type]:
    if node is None:
        return {}
    mapping = {"str": str, "int": int, "float": float, "bool": bool}
    return {name: mapping.get(kind, str) for name, kind in node.outputs.items()}


def classify(exc: BaseException) -> str:
    from ao.gateway import BudgetExceeded
    from ao.vars import VarError
    from wf_sdk import ContractError

    if isinstance(exc, InputGuardError):
        # 输入中和失败属于安全拒绝，重试也不会变好（sandbox_denied 在 NO_RETRY 里）
        return "sandbox_denied"
    if isinstance(exc, BudgetExceeded):
        return "budget_exceeded"
    if isinstance(exc, VarError):
        return exc.kind
    if isinstance(exc, RunCancelled):
        return "cancelled"
    if isinstance(exc, FencedError):
        return "fenced"
    if isinstance(exc, ContractError):
        return "contract_error"
    if isinstance(exc, ProviderError):
        return "provider_error"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, PermissionError):
        return "sandbox_denied"
    if isinstance(exc, ValueError):
        return "template_error"
    return "internal_error"


# --- 图构建 -------------------------------------------------------------------


def build_state_graph(manifest: Manifest, runners: dict[str, Any]) -> Any:
    """从 manifest 声明直接构图。

    运行期不再加载任何生成代码：节点就是 `runners[id]`，边来自 manifest，
    入口取"没有入边的节点"（多个即天然并行）。
    """
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(GraphState)
    for node in manifest.nodes:
        builder.add_node(node.id, runners[node.id])

    outgoing: dict[str, list[tuple[str, str | None]]] = {}
    for edge in manifest.edges:
        outgoing.setdefault(edge.src, []).append((edge.dst, edge.when))

    for node in manifest.nodes:
        edges = outgoing.get(node.id, [])
        if not edges:
            builder.add_edge(node.id, END)
            continue
        if any(when for _, when in edges):

            def router(
                state: dict[str, Any], _edges: list[tuple[str, str | None]] = edges
            ) -> list[str]:
                nodes = state.get("nodes") or {}
                targets = [
                    dst for dst, when in _edges if not when or eval_when(when, nodes)
                ]
                return targets or [END]

            builder.add_conditional_edges(node.id, router)
        else:
            for dst, _ in edges:
                builder.add_edge(node.id, dst)

    incoming = {edge.dst for edge in manifest.edges}
    for node in manifest.nodes:
        if node.id not in incoming:
            builder.add_edge(START, node.id)
    return builder


def stage_runner(node: Node) -> Any:
    """一个阶段的执行体：组装上下文 → 交给 runtime 跑本阶段的多轮 → 推进图状态。

    这里没有任何生成代码。节点的"做什么"来自 prompts 文件与 manifest 声明，
    "怎么做"由 harness 会话完成，平台只负责边界（会话新建、产物校验、落库）。
    """

    async def runner(state: dict[str, Any]) -> dict[str, Any]:
        env = current_run_env()
        ctx = NodeContext(
            run_id=str(state.get("run_id") or ""),
            node_id=node.id,
            attempt=env.attempt,
            vars=dict(state.get("vars") or {}),
            nodes=dict(state.get("nodes") or {}),
            subject=dict(state.get("subject") or {}),
            package_root=env.package_root,
            workspace=env.workspace,
            artifacts=env.artifacts,
            inputs=env.inputs,
            mcp=list(env.mcp),
            skills=list(env.skills),
            runtime=env.runtime,
        )
        token = set_context(ctx)
        try:
            if ctx.runtime is not None:
                ctx.runtime.node_started(ctx, node)
            data = await ctx.runtime.run_stage(ctx, node) if ctx.runtime else {}
            if ctx.runtime is not None:
                ctx.runtime.node_finished(ctx, node, data)
                # 阶段边界落摘要：下一个节点是新会话，只能从这里读到本阶段的结论
                await ctx.runtime.summarize_stage(ctx, node, data)
            nodes = dict(ctx.nodes)
            nodes[node.id] = data
            return {"nodes": nodes, "last": node.id}
        except Exception as exc:
            if ctx.runtime is not None:
                ctx.runtime.node_failed(ctx, node, exc)
            raise
        finally:
            reset_context(token)

    runner.__name__ = f"stage_{node.id}"
    return runner


def _finish(run_id: str, epoch: int, status: str, error: str | None = None) -> None:
    with tx() as conn:
        updated = conn.execute(
            "UPDATE runs SET status=%s, error=%s, ended_at=now(), current_node=NULL,"
            " lease_owner=NULL, lease_until=NULL"
            " WHERE id=%s AND epoch=%s AND status NOT IN ('succeeded','failed','cancelled')",
            (status, error, run_id, epoch),
        )
        if updated.rowcount:
            events.emit(
                conn,
                run_id,
                "run.finished",
                payload={"status": status, "error": error, "epoch": epoch},
            )


async def execute(run_id: str) -> str:
    """执行一个 run，返回终态。异常不会外抛（由状态机留痕）。"""
    run = fetch_one("SELECT * FROM runs WHERE id = %s", (run_id,))
    if run is None:
        raise ValueError(f"run 不存在: {run_id}")
    epoch = int(run["epoch"])
    if run["status"] in {"succeeded", "failed", "cancelled"}:
        return str(run["status"])

    try:
        package = load_package(run["package_ref"])
        manifest = parse(package.manifest)
        provider = current_provider()
        runtime = RunRuntime(run=run, manifest=manifest, package=package, provider=provider)

        workspace = package.root / "workspace"
        artifacts = package.root / "artifacts"
        workspace.mkdir(exist_ok=True)
        artifacts.mkdir(exist_ok=True)
        env = RunEnv(
            package_root=package.root,
            workspace=workspace,
            artifacts=artifacts,
            inputs=None,
            runtime=runtime,
            attempt=int(run.get("retry_count") or 0),
            mcp=list(runtime.mcp_servers),
            skills=list(runtime.skill_refs),
        )
        token = set_run_env(env)
        try:
            # run 级变量在 run 开始时一次解析
            run_values, run_calls = runtime.resolve_run_vars(
                {"subject": run.get("subject") or {}}
            )
            runtime.emit_var_calls(None, run_calls)
            graph_state: GraphState = {
                "run_id": run_id,
                "attempt": env.attempt,
                "vars": dict(run_values),
                "nodes": {},
                "subject": run.get("subject") or {},
            }
            compiled = build_state_graph(
                manifest, {node.id: stage_runner(node) for node in manifest.nodes}
            )
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            async with AsyncPostgresSaver.from_conn_string(settings.db_url) as saver:
                await saver.setup()
                app = compiled.compile(checkpointer=saver)
                config = {
                    "configurable": {"thread_id": run_id},
                    "recursion_limit": max(10, manifest.limits.max_nodes * 4),
                }
                # checkpoint 粒度是节点之间：重试 / 接管时从上一个节点之后续跑，
                # 不重放已完成节点（否则外部写会重复，虽然 action 幂等，但没必要）。
                snapshot = await app.aget_state(config)
                resume = bool(snapshot and snapshot.next)
                await asyncio.wait_for(
                    app.ainvoke(None if resume else graph_state, config),
                    timeout=manifest.limits.max_wall_seconds,
                )
        finally:
            reset_run_env(token)

        _finish(run_id, epoch, "succeeded")
        return "succeeded"
    except BaseException as exc:  # noqa: BLE001 — 含 asyncio.TaskGroup 抛出的 BaseExceptionGroup
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        leaf = _leaf(exc)
        if isinstance(leaf, RunCancelled):
            _finish(run_id, epoch, "cancelled", "用户请求停止")
            return "cancelled"
        if isinstance(leaf, FencedError):
            # 不是本进程的 run 了：什么都不写，交给新 owner
            logger.warning("fencing 拒绝写入，放弃执行: run=%s error=%s", run_id, leaf)
            return "fenced"
        logger.exception("run 执行异常: %s", run_id)
        # 失败分类决定"回队列退避 / 一次性重试 / 终态"；402 与鉴权不进这里。
        return state_machine.schedule_retry(run_id, code=classify(leaf), error=str(leaf))


def _leaf(exc: BaseException) -> BaseException:
    """LangGraph 会把节点异常包进 ExceptionGroup；取第一个叶子做分类。"""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc
