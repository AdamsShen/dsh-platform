"""P1-T05 容量采样：串行 + 并发各跑若干真实 DSH 轮次，产出延迟与成功率。

只测执行侧容量（进程冷启动 + 单轮往返），不测网关计费：
账单以网关 spend log 为准，本机没有网关时不臆造 token 成本。

用法：
    DEEPSEEK_API_KEY=... uv run python scripts/capacity_report.py --seq 3 --conc 4
结果同时写到 docs/reports/capacity-<日期>.md（--report 可改路径）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import tempfile
import time
from datetime import datetime
from pathlib import Path

from ao.harness.dsh import DshProvider
from ao.settings import PROJECT_ROOT, settings

PROMPT = (
    "请只输出一个 ```result 代码块，内容为 "
    '{"ok": true, "answer": "pong"}，不要调用任何工具，不要输出其它文字。'
)
_RESULT_RE = re.compile(r"```result\s*(\{.*?\})\s*```", re.DOTALL)


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return ordered[index]


async def one_turn(provider: DshProvider, tag: str) -> dict:
    cwd = Path(tempfile.mkdtemp(prefix=f"ao-cap-{tag}-"))
    started = time.perf_counter()
    reply = await provider.send(
        run_id=f"cap_{tag}",
        node_id="probe",
        attempt=0,
        system="你是一个只负责按要求输出 JSON 的执行节点。",
        prompt=PROMPT,
        contract=None,
        context={"cwd": str(cwd), "capability": "read_only", "timeout": 240},
    )
    elapsed = time.perf_counter() - started
    parsed = bool(_RESULT_RE.search(reply.text or ""))
    return {
        "tag": tag,
        "status": reply.status,
        "elapsed": round(elapsed, 3),
        "contract_ok": parsed,
        "error": (reply.meta or {}).get("error"),
    }


def summarize(label: str, rows: list[dict]) -> dict:
    times = [row["elapsed"] for row in rows]
    ok = [row for row in rows if row["status"] == "ok" and row["contract_ok"]]
    return {
        "label": label,
        "n": len(rows),
        "ok": len(ok),
        "success_rate": round(len(ok) / len(rows), 3) if rows else 0.0,
        "p50": round(statistics.median(times), 3) if times else 0.0,
        "p95": round(percentile(times, 0.95), 3) if times else 0.0,
        "min": round(min(times), 3) if times else 0.0,
        "max": round(max(times), 3) if times else 0.0,
        "wall": round(sum(times), 3) if times else 0.0,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seq", type=int, default=3, help="串行轮次数")
    parser.add_argument("--conc", type=int, default=4, help="并发轮次数")
    parser.add_argument("--report", default="", help="报告输出路径")
    args = parser.parse_args()

    provider = DshProvider()
    status = provider.probe()
    print(f"[probe] {status.status} {status.detail}")
    if status.status != "ok":
        return 2

    sequential = []
    for i in range(args.seq):
        row = await one_turn(provider, f"s{i}")
        sequential.append(row)
        print(f"[seq {i}] {row}")
        await asyncio.sleep(1)

    started = time.perf_counter()
    concurrent = list(
        await asyncio.gather(*(one_turn(provider, f"c{i}") for i in range(args.conc)))
    )
    batch_wall = time.perf_counter() - started
    for row in concurrent:
        print(f"[conc] {row}")

    seq_summary = summarize("sequential", sequential)
    conc_summary = summarize("concurrent", concurrent)
    conc_summary["batch_wall"] = round(batch_wall, 3)
    conc_summary["throughput_per_min"] = (
        round(len(concurrent) / batch_wall * 60, 2) if batch_wall else 0.0
    )

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": settings.dsh_model or "default(deepseek-v4-flash)",
        "profile": settings.dsh_profile,
        "sandbox": "read_only",
        "sequential": seq_summary,
        "concurrent": conc_summary,
        "raw": sequential + concurrent,
    }
    print("[summary] " + json.dumps(payload, ensure_ascii=False))

    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(payload), encoding="utf-8")
        print(f"[report] {path}")
    return 0


def render_markdown(payload: dict) -> str:
    seq = payload["sequential"]
    conc = payload["concurrent"]
    rows = "\n".join(
        f"| {r['tag']} | {r['status']} | {r['elapsed']:.2f}"
        f" | {'是' if r['contract_ok'] else '否'} |"
        for r in payload["raw"]
    )
    seq_line = (
        f"| 串行 | {seq['n']} | {seq['ok']} | {seq['p50']:.2f}s"
        f" | {seq['p95']:.2f}s | {seq['min']:.2f}s | {seq['max']:.2f}s |"
    )
    conc_line = (
        f"| 并发 {conc['n']} 路 | {conc['n']} | {conc['ok']} | {conc['p50']:.2f}s"
        f" | {conc['p95']:.2f}s | {conc['min']:.2f}s | {conc['max']:.2f}s |"
    )
    return f"""# DSH 容量采样（{payload['generated_at']}）

- 模型：{payload['model']}（profile={payload['profile']}）
- 沙箱能力：{payload['sandbox']}（无工具调用，仅单轮 JSON 输出）
- 账单：本机未接网关，**不含 token 成本**；成本真值以 LiteLLM spend log 为准

## 汇总

| 场景 | 轮次 | 成功 | p50 | p95 | min | max |
|---|---|---|---|---|---|---|
{seq_line}
{conc_line}

并发批次墙钟 {conc.get('batch_wall', 0):.2f}s，吞吐约 {conc.get('throughput_per_min', 0)} 轮/分钟。

## 明细

| 轮次 | 状态 | 耗时(s) | 契约可解析 |
|---|---|---|---|
{rows}

## 解读与限制

1. 每轮都会新起 DSH 子进程（Session.run 无 cancel，只能跑完即关），因此 **p50 里含固定冷启动**；
   进程内多轮的容量应显著更高，但当前 provider 设计就是一轮一进程。
2. 采样量很小（{seq['n'] + conc['n']} 轮），只够判断"能否跑、量级多少"，不能当容量上限。
3. 未覆盖工具调用、MCP 取数、长上下文与大产物落盘，这些会显著拉长单轮时间。
4. `AO_EXEC_SLOTS` 应取"并发 p95 × 安全系数"的倒数关系估算，等接上网关后用真实 spend log 再校准。
"""


if __name__ == "__main__":
    report = PROJECT_ROOT / "docs" / "reports" / f"capacity-{datetime.now().date().isoformat()}.md"
    import sys

    sys.argv = [*sys.argv, "--report", str(report)] if "--report" not in sys.argv else sys.argv
    raise SystemExit(asyncio.run(main()))
