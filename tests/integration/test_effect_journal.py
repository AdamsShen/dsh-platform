from __future__ import annotations

import pytest

from ao.db import fetch_one, tx
from ao.effects import EffectInFlight, params_hash, run_once
from tests.helpers import make_run


def _insert_started(
    run_id: str, *, epoch: int, age_seconds: int = 0, params: dict | None = None
) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO run_effects (run_id, node_id, action, params_hash,"
            " owner_epoch, attempt, status, updated_at)"
            " VALUES (%s,'n1','notify',%s,%s,0,'started', now() - make_interval(secs => %s))",
            (run_id, params_hash(params or {}), epoch, age_seconds),
        )


@pytest.mark.integration
async def test_action_runs_once_and_replays_result() -> None:
    run_id = make_run(window="fx1")
    calls: list[int] = []

    def fn() -> dict[str, int]:
        calls.append(1)
        return {"sent": 1}

    first = await run_once(
        run_id=run_id, node_id="n1", action="notify", params={"n": 1}, epoch=0, fn=fn
    )
    again = await run_once(
        run_id=run_id, node_id="n1", action="notify", params={"n": 1}, epoch=0, fn=fn
    )

    assert first == again == {"sent": 1}
    assert len(calls) == 1, "重跑必须返回旧结果而不是再执行一次"
    row = fetch_one(
        "SELECT status, attempt, result FROM run_effects WHERE run_id=%s AND params_hash=%s",
        (run_id, params_hash({"n": 1})),
    )
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["attempt"] == 0
    assert row["result"] == {"sent": 1}


@pytest.mark.integration
async def test_different_params_are_independent_effects() -> None:
    run_id = make_run(window="fx2")
    calls: list[int] = []

    def fn(n: int) -> int:
        calls.append(n)
        return n * 10

    a = await run_once(
        run_id=run_id, node_id="n1", action="notify", params={"n": 1}, epoch=0,
        fn=lambda: fn(1),
    )
    b = await run_once(
        run_id=run_id, node_id="n1", action="notify", params={"n": 2}, epoch=0,
        fn=lambda: fn(2),
    )
    assert (a, b) == (10, 20)
    assert calls == [1, 2]


@pytest.mark.integration
async def test_failure_is_recorded_and_retryable() -> None:
    run_id = make_run(window="fx3")
    calls: list[int] = []

    def boom() -> str:
        calls.append(1)
        raise RuntimeError("上游 500")

    with pytest.raises(RuntimeError, match="上游 500"):
        await run_once(
            run_id=run_id, node_id="n1", action="notify", params={"n": 1}, epoch=0, fn=boom
        )
    row = fetch_one("SELECT status, error FROM run_effects WHERE run_id=%s", (run_id,))
    assert row is not None and row["status"] == "failed"
    assert "上游 500" in row["error"]

    # 失败不是终态：同一键再调一次必须真执行
    value = await run_once(
        run_id=run_id, node_id="n1", action="notify", params={"n": 1}, epoch=0,
        fn=lambda: "ok",
    )
    assert value == "ok"
    assert calls == [1]


@pytest.mark.integration
async def test_same_epoch_in_flight_blocks_duplicate() -> None:
    run_id = make_run(window="fx4")
    _insert_started(run_id, epoch=2)  # 同一 owner 刚开始跑

    with pytest.raises(EffectInFlight):
        await run_once(
            run_id=run_id, node_id="n1", action="notify", params={}, epoch=2,
            fn=lambda: "should not run",
        )


@pytest.mark.integration
async def test_old_epoch_takes_over_without_waiting() -> None:
    run_id = make_run(window="fx5")
    _insert_started(run_id, epoch=1)  # 上一任 owner 已失权

    value = await run_once(
        run_id=run_id, node_id="n1", action="notify", params={}, epoch=2, fn=lambda: "taken"
    )
    assert value == "taken"
    row = fetch_one(
        "SELECT status, owner_epoch, attempt FROM run_effects WHERE run_id=%s", (run_id,)
    )
    assert row is not None
    assert (row["status"], row["owner_epoch"], row["attempt"]) == ("succeeded", 2, 1)


@pytest.mark.integration
async def test_stale_same_epoch_takes_over() -> None:
    run_id = make_run(window="fx6")
    _insert_started(run_id, epoch=2, age_seconds=7200)  # 同一 epoch 但早就没动静

    value = await run_once(
        run_id=run_id, node_id="n1", action="notify", params={}, epoch=2, fn=lambda: "recovered"
    )
    assert value == "recovered"
