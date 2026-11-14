from __future__ import annotations

import pytest

from ao.db import fetch_one, tx
from ao.queue import claim_next, heartbeat, reclaim_expired
from tests.helpers import make_run


@pytest.mark.integration
def test_claim_is_exclusive() -> None:
    run_id = make_run(window="q1")
    first = claim_next("worker-a")
    assert first is not None and first["id"] == run_id
    assert claim_next("worker-b") is None

    row = fetch_one("SELECT status, lease_owner, lease_until FROM runs WHERE id = %s", (run_id,))
    assert row["status"] == "running"
    assert row["lease_owner"] == "worker-a"
    assert row["lease_until"] is not None


@pytest.mark.integration
def test_expired_lease_is_reclaimed_with_new_epoch() -> None:
    run_id = make_run(window="q2")
    claimed = claim_next("worker-a")
    assert claimed is not None and int(claimed["epoch"]) == 0

    with tx() as conn:
        conn.execute(
            "UPDATE runs SET lease_until = now() - interval '1 second' WHERE id = %s", (run_id,)
        )
    reclaimed = reclaim_expired()
    assert [row["id"] for row in reclaimed] == [run_id]
    assert int(reclaimed[0]["epoch"]) == 1

    again = claim_next("worker-b")
    assert again is not None and again["id"] == run_id
    assert int(again["epoch"]) == 1


@pytest.mark.integration
def test_retry_at_blocks_claim() -> None:
    run_id = make_run(window="q3")
    with tx() as conn:
        conn.execute(
            "UPDATE runs SET retry_at = now() + interval '1 hour' WHERE id = %s", (run_id,)
        )
    assert claim_next("worker-a") is None


@pytest.mark.integration
def test_heartbeat_upserts_and_renews() -> None:
    run_id = make_run(window="q4")
    claim_next("worker-a")
    with tx() as conn:
        conn.execute(
            "UPDATE runs SET lease_until = now() + interval '1 second' WHERE id = %s", (run_id,)
        )
    heartbeat("worker-a", host="h1", slots=2)
    row = fetch_one("SELECT slots FROM worker_heartbeats WHERE worker_id = 'worker-a'")
    assert row["slots"] == 2
    run = fetch_one(
        "SELECT lease_until > now() + interval '20 seconds' AS long FROM runs WHERE id = %s",
        (run_id,),
    )
    assert run["long"] is True
