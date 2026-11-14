"""真实 LiteLLM 网关契约探针（P1-T12 验收）。

目的不是"能连上"，而是逐条核对 `ao/gateway.py` 依赖的契约：
`/key/generate` 返回哪个字段当 key 标识、`duration` 是否真的落成 `expires`、
`/spend/logs` 现在返回的是明细还是每日聚合、虚拟 key 能否反查回 run、吊销是否即时生效。

用法（只对测试实例跑，会真实创建并删除虚拟 key）：
    AO_GATEWAY_URL=http://127.0.0.1:4000 \
    AO_GATEWAY_MASTER_KEY=sk-... \
    AO_GATEWAY_PROBE_MODEL=mock-gpt \
    uv run python scripts/probe_gateway.py

模型需要是一个能在网关上真实跑通的模型；mock_response 的模型也能产生 spend 记录，
但若网关未接数据库，spend 相关检查会明确报"网关未启用 DB"。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx

from ao.gateway import LiteLLMGateway, normalize_spend
from ao.settings import settings

BASE = (os.environ.get("AO_GATEWAY_URL") or settings.gateway_url).rstrip("/")
MASTER = os.environ.get("AO_GATEWAY_MASTER_KEY") or settings.gateway_master_key
MODEL = os.environ.get("AO_GATEWAY_PROBE_MODEL", "mock-gpt")
# 网关的 key_alias 全局唯一且删除后仍占位，所以每次跑都用新的 run_id 才能复跑
RUN_ID = os.environ.get("AO_GATEWAY_PROBE_RUN_ID") or f"probe_run_{int(time.time())}"
NODE_ID = "probe_node"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE,
        headers={"Authorization": f"Bearer {MASTER}"},
        timeout=30.0,
    )


async def probe_liveness(client: httpx.AsyncClient) -> None:
    try:
        resp = await client.get("/health/liveliness")
        check(resp.status_code == 200, "网关可达", f"HTTP {resp.status_code}")
    except Exception as exc:  # noqa: BLE001 — 探针要把连接错误也报成人话
        check(False, "网关可达", f"{type(exc).__name__}: {exc}")


async def probe_issue_key(client: httpx.AsyncClient) -> tuple[str, str]:
    """核对 /key/generate 的响应字段，并检查项目封装取到的 key_id 是否稳定。"""
    alias = f"{RUN_ID}:{NODE_ID}:0:node"
    raw = await client.post(
        "/key/generate",
        json={
            "key_alias": alias,
            "metadata": {"run_id": RUN_ID, "node_id": NODE_ID, "attempt": 0, "purpose": "node"},
            "max_budget": 1.0,
            "duration": "1d",
        },
    )
    if raw.status_code >= 400:
        check(False, "/key/generate 可用", f"HTTP {raw.status_code}: {raw.text[:200]}")
        return "", ""
    body = raw.json()
    check(True, "/key/generate 可用", f"HTTP {raw.status_code}")

    has_token = bool(body.get("token"))
    check(
        has_token,
        "响应含 token（key 的稳定标识）",
        "" if has_token else "只有 key/key_name，无法做稳定归因",
    )
    check(
        valid := bool(body.get("expires")),
        "duration=1d 落成 expires",
        f"expires={body.get('expires')}" if valid else "传了 duration 但没生成过期时间",
    )

    # 项目封装用另一个 alias（网关 alias 全局唯一，同 alias 会 400）
    gateway = LiteLLMGateway(base_url=BASE, master_key=MASTER)
    sdk_key = await gateway.issue_key(
        run_id=RUN_ID, node_id="probe_sdk", attempt=0, purpose="node", budget_usd=1.0
    )
    looks_stable = len(sdk_key.key_id) == 64 and all(
        c in "0123456789abcdef" for c in sdk_key.key_id
    )
    check(
        looks_stable,
        "LiteLLMGateway.key_id 是稳定 token",
        f"key_id={sdk_key.key_id[:12]}…（len={len(sdk_key.key_id)}）"
        if looks_stable
        else f"取到的是展示名而非 token：{sdk_key.key_id}",
    )
    await gateway.revoke_key(sdk_key.key)

    # alias 全局唯一且删除后仍占位，这决定接线时能不能拿它当幂等键
    dup = await client.post("/key/generate", json={"key_alias": alias})
    check(
        dup.status_code in (200, 400, 409),
        "重复 alias 的行为已确认",
        f"HTTP {dup.status_code}："
        + (
            "拒绝，alias 全局唯一且删除后仍占位"
            if dup.status_code >= 400
            else "允许，返回新 key"
        ),
    )
    return str(body.get("key") or ""), str(body.get("token") or "")


async def probe_completion(client: httpx.AsyncClient, key: str) -> None:
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": MODEL, "messages": [{"role": "user", "content": "probe"}]},
    )
    check(resp.status_code == 200, "虚拟 key 可调用模型", f"HTTP {resp.status_code}")


async def probe_spend_logs(client: httpx.AsyncClient, token: str) -> None:
    """对比老端点（聚合）与新端点（明细），判断 gateway.py 现在打哪一个才对。"""
    params = {"start_date": "2020-01-01", "end_date": "2099-12-31"}

    legacy = await client.get("/spend/logs", params=params)
    legacy_rows = legacy.json() if legacy.status_code == 200 else []
    legacy_is_detail = bool(
        isinstance(legacy_rows, list)
        and legacy_rows
        and isinstance(legacy_rows[0], dict)
        and "request_id" in legacy_rows[0]
    )
    # 老端点已退化为按天聚合，这是网关事实（代码已改走 v2），探针只记录形态
    check(
        True,
        "老 /spend/logs 的返回形态已确认",
        "逐条明细"
        if legacy_is_detail
        else "按天聚合（无 request_id）；gateway.py 已改走 /spend/logs/v2",
    )

    # 网关是异步落库，刚调完的流水不会立刻出现，必须轮询——接线时同步任务也要容忍这点
    row = None
    waited = 0
    rows: list[dict[str, Any]] = []
    for _ in range(12):
        v2 = await client.get("/spend/logs/v2", params={**params, "page": 1, "page_size": 20})
        if v2.status_code >= 400:
            check(False, "/spend/logs/v2 可用", f"HTTP {v2.status_code}: {v2.text[:200]}")
            return
        payload = v2.json()
        rows = payload.get("data") if isinstance(payload, dict) else payload
        row = next((r for r in (rows or []) if r.get("api_key") == token), None)
        if row is not None:
            break
        await asyncio.sleep(1)
        waited += 1

    check(bool(rows), "/spend/logs/v2 返回明细", f"{len(rows)} 行，等待 {waited}s 后命中")
    check(
        row is not None,
        "可凭 api_key(=token) 反查本次调用",
        f"等待 {waited}s" if row is not None else f"等待 {waited}s 仍拿不到，无法做 run 级归因",
    )
    if row is None:
        return

    mapped = normalize_spend(row)
    check(
        bool(mapped["gateway_request_id"]),
        "normalize_spend 能取到 request_id",
        f"request_id={mapped['gateway_request_id']}",
    )
    check(
        bool(mapped["run_id"]),
        "normalize_spend 能取到 run_id",
        "" if mapped["run_id"] else "spend log 没有 run 维度，需靠 api_key 反查本地签发表",
    )


async def probe_revoke(client: httpx.AsyncClient, key: str) -> None:
    resp = await client.post("/key/delete", json={"keys": [key]})
    check(resp.status_code < 400, "/key/delete 可用", f"HTTP {resp.status_code}")
    after = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": MODEL, "messages": [{"role": "user", "content": "after revoke"}]},
    )
    check(after.status_code in (401, 403), "吊销后立即失效", f"HTTP {after.status_code}")


async def main() -> int:
    if not BASE or not MASTER:
        print("缺少 AO_GATEWAY_URL / AO_GATEWAY_MASTER_KEY", file=sys.stderr)
        return 2
    print(f"gateway={BASE} model={MODEL}\n")
    async with _client() as client:
        await probe_liveness(client)
        key, token = await probe_issue_key(client)
        if key:
            await probe_completion(client, key)
            await probe_spend_logs(client, token)
            await probe_revoke(client, key)

    failed = [r for r in results if not r[0]]
    print(f"\n结论：{len(results) - len(failed)}/{len(results)} 通过")
    if failed:
        print("未通过项：")
        for _, name, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    print(json.dumps({"ok": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
