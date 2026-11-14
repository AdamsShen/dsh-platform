"""run 执行器：加载冻结包 → 构建 LangGraph → 逐节点执行 → 记账/事件/终态。

不变量：
- 一个节点 = 一次 harness turn（repair 是同一 attempt 内的第二次 turn）；
- 图状态只管"走到哪"，DSH 会话只管"说到哪句"，两者在 `run_steps.native_id` 对齐；
- 所有写库都带 `epoch` 条件（fencing），旧进程写不进去；
- 停止只在节点边界生效。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import socket
import sys
from typing import Any, TypedDict

from psycopg.types.json import Jsonb

from ao import contracts, events, resources, usage
from ao import state as state_machine
from ao.db import fetch_one, read, tx
from ao.harness.base import Provider, Reply
from ao.harness.mock import MockProvider
from ao.manifest import Manifest, Node, eval_when, parse
from ao.package import Package, load_package
from ao.settings import settings
from wf_sdk.runtime import GraphSpec, RunEnv, reset_run_env, set_run_env

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
        # 工作流级绑定：MCP 配置在这里解析一次，所有 agent 节点共享；
        # 发布期已校验，这里再解析是兜底（资源可能发布后被下架）
        self.mcp_servers, self.skill_refs, skipped = resources.resolve_bindings(
            manifest.effective_bindings(), user_id=run.get("trigger_user_id")
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

    def assert_action_allowed(self, ctx: Any) -> None:
        node = self.manifest.node(ctx.node_id)
        if node is None or node.kind != "action":
            raise PermissionError(f"节点 {ctx.node_id} 不是 action 节点，禁止调用平台动作")

    async def call_agent(
        self,
        ctx: Any,
        *,
        prompt: str,
        vars: dict[str, Any],  # noqa: A002 — 与生成代码参数名保持一致
        mcp: list[str],
        skills: list[str],
    ) -> str:
        return await self._turn(ctx, prompt=prompt, repair=False)

    async def _turn(self, ctx: Any, *, prompt: str, repair: bool) -> str:
        node = self.manifest.node(ctx.node_id)
        timeout = float(node.timeout if node else 600)
        system = ctx.system()
        if self.current_node and self.current_node.kind == "agent":
            # system 序言由 provider 拼接，且必须留痕（见设计 §7.2）
            pass
        reply: Reply = await self.provider.send(
            run_id=self.run_id,
            node_id=ctx.node_id,
            attempt=ctx.attempt,
            system=system,
            prompt=prompt,
            contract=contracts.contract_of({k: v.__name__ for k, v in _outputs_of(node).items()}),
            repair=repair,
            native_id=self.native_id,
            context={
                "capability": getattr(node, "capability", "read_only"),
                "cwd": str(self.package.root),
                "timeout": timeout,
                # 工作流级工具：provider 侧把它们拼进 harness 的 MCP 补丁
                "mcp_servers": self.mcp_servers,
                "skills": self.skill_refs,
            },
            on_activity=lambda event: self._on_activity(ctx, event),
        )
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

    async def repair(self, ctx: Any, spec: Any, errors: list[str]) -> dict[str, Any]:
        self.repair_count += 1
        with tx() as conn:
            events.emit(
                conn,
                self.run_id,
                "node.repair",
                node_id=ctx.node_id,
                payload={"errors": errors},
            )
        outputs = {name: kind.__name__ for name, kind in spec.outputs.items()}
        text = await self._turn(
            ctx, prompt=contracts.repair_prompt(errors, outputs), repair=True
        )
        data, remaining = contracts.validate(text, outputs)
        if remaining:
            raise contracts_error(remaining)
        return data or {}


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
    from ao.sandbox import SandboxUnavailable
    from ao.vars import VarError
    from wf_sdk import ContractError

    if isinstance(exc, SandboxUnavailable):
        return "sandbox_unavailable"
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


def build_state_graph(spec: GraphSpec) -> Any:
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(GraphState)
    for node_spec, fn in spec.nodes:
        builder.add_node(node_spec.id, fn)

    outgoing: dict[str, list[dict[str, str]]] = {}
    for edge in spec.edges:
        outgoing.setdefault(edge["from"], []).append(edge)

    for node_spec, _ in spec.nodes:
        edges = outgoing.get(node_spec.id, [])
        if not edges:
            builder.add_edge(node_spec.id, END)
            continue
        if any(edge.get("when") for edge in edges):
            def router(state: dict[str, Any], _edges: list[dict[str, str]] = edges) -> list[str]:
                nodes = state.get("nodes") or {}
                targets = [
                    edge["to"]
                    for edge in _edges
                    if not edge.get("when") or eval_when(edge["when"], nodes)
                ]
                return targets or [END]

            builder.add_conditional_edges(node_spec.id, router)
        else:
            for edge in edges:
                builder.add_edge(node_spec.id, edge["to"])

    incoming = {edge["to"] for edge in spec.edges}
    entries = spec.entry or [n.id for n, _ in spec.nodes if n.id not in incoming]
    for entry in entries:
        builder.add_edge(START, entry)
    return builder


def _check_spec(spec: GraphSpec, manifest: Manifest) -> None:
    ids = set(spec.node_ids())
    expected = {node.id for node in manifest.nodes}
    if ids != expected:
        raise ValueError(
            f"graph.py 节点集合与 manifest 不一致: 代码 {sorted(ids)}，manifest {sorted(expected)}"
        )
    edges = set(spec.edge_pairs())
    expected_edges = {(edge.src, edge.dst) for edge in manifest.edges}
    if edges != expected_edges:
        raise ValueError("graph.py 边集合与 manifest 不一致")


def _load_graph_module(package: Package, run_id: str) -> Any:
    name = f"ao_pkg_{run_id}"
    module_spec = importlib.util.spec_from_file_location(name, package.graph_path)
    if module_spec is None or module_spec.loader is None:
        raise ValueError(f"无法加载 {package.graph_path}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[name] = module
    module_spec.loader.exec_module(module)
    return module


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
        from ao.sandbox import ensure_available

        if not settings.mock:
            # fail closed：真跑时后端不可用就不执行；mock 不起子进程，不受沙箱限制
            ensure_available()
        provider = current_provider()
        runtime = RunRuntime(run=run, manifest=manifest, package=package, provider=provider)

        module = _load_graph_module(package, run_id)
        spec = getattr(module, "G", None)
        if not isinstance(spec, GraphSpec):
            raise ValueError("graph.py 必须定义模块级变量 G = graph(nodes=[...], edges=[...])")
        _check_spec(spec, manifest)

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
            compiled = build_state_graph(spec)
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
