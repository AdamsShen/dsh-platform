from __future__ import annotations

import pytest

from ao.db import fetch_one
from ao.queue import claim_next
from ao.state import request_cancel, schedule_retry
from tests.helpers import make_run


@pytest.mark.integration
def test_cancel_queued_is_immediate() -> None:
    run_id = make_run(window="s1")
    assert request_cancel(run_id, actor="user:u1") == "cancelled"
    assert fetch_one("SELECT status FROM runs WHERE id = %s", (run_id,))["status"] == "cancelled"


@pytest.mark.integration
def test_cancel_running_waits_for_boundary() -> None:
    run_id = make_run(window="s2")
    claim_next("worker-a")
    assert request_cancel(run_id) == "cancelling"
    row = fetch_one("SELECT status, cancel_requested FROM runs WHERE id = %s", (run_id,))
    assert row["status"] == "running"
    assert row["cancel_requested"] is True


@pytest.mark.integration
def test_transient_failure_retries_then_exhausts() -> None:
    run_id = make_run(window="s3")
    claim_next("worker-a")
    assert schedule_retry(run_id, code="provider_error", error="boom", backoff=0) == "queued"
    row = fetch_one("SELECT retry_count FROM runs WHERE id = %s", (run_id,))
    assert row["retry_count"] == 1

    for expected_count in (2, 3):
        claim_next("worker-a")
        schedule_retry(run_id, code="provider_error", error="boom", backoff=0)
        row = fetch_one("SELECT retry_count FROM runs WHERE id = %s", (run_id,))
        assert row["retry_count"] == expected_count

    claim_next("worker-a")
    assert schedule_retry(run_id, code="provider_error", error="boom") == "failed"


@pytest.mark.integration
def test_contract_error_is_terminal() -> None:
    run_id = make_run(window="s4")
    claim_next("worker-a")
    assert schedule_retry(run_id, code="contract_error", error="bad") == "failed"
    assert fetch_one("SELECT retry_count FROM runs WHERE id = %s", (run_id,))["retry_count"] == 0


@pytest.mark.integration
def test_missing_var_retries_once() -> None:
    run_id = make_run(window="s5")
    claim_next("worker-a")
    assert schedule_retry(run_id, code="missing_var", error="no data") == "queued"
    claim_next("worker-a")
    assert schedule_retry(run_id, code="missing_var", error="no data") == "failed"
