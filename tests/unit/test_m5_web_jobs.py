"""M5.6 persistent job lease, recovery, event and worker tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from award_audit.core.pipeline.store import StateConflictError, Store
from award_audit.web.jobs import JobRepository, JobWorker


def test_file_store_enables_wal_and_busy_timeout(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "wal.db")
    assert str(store.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    assert int(store.conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
    memory = Store(":memory:")
    assert str(memory.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "memory"


def test_job_claim_is_unique_across_connections_and_events_are_replayable(tmp_path) -> None:  # noqa: ANN001
    db_path = tmp_path / "jobs.db"
    first_store = Store(db_path)
    first = JobRepository(first_store)
    job = first.enqueue("fake", {"value": 1}, created_by="reviewer")
    second_store = Store(db_path)
    second = JobRepository(second_store)

    claimed = first.claim_next("worker-one", lease_seconds=30)
    assert claimed is not None and claimed.job_id == job.job_id
    assert second.claim_next("worker-two", lease_seconds=30) is None
    first.progress(job.job_id, "worker-one", 40, "正在处理")
    completed = first.finish(
        job.job_id,
        "worker-one",
        __import__("award_audit.web.models", fromlist=["JobOutcome"]).JobOutcome(
            result={"ok": True}
        ),
    )
    assert completed.status == "completed" and completed.progress == 100
    events = second.events_after(0)
    assert [item.event_type for item in events] == [
        "job.queued", "job.running", "job.progress", "job.completed"
    ]
    assert second.events_after(events[1].event_id)[0].event_type == "job.progress"


def test_expired_lease_requeues_or_fails_at_attempt_limit(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "recovery.db")
    jobs = JobRepository(store)
    retryable = jobs.enqueue("fake", {}, created_by="r", max_attempts=2)
    exhausted = jobs.enqueue("fake", {}, created_by="r", max_attempts=1)
    jobs.claim_next("worker-a", lease_seconds=30)
    jobs.claim_next("worker-a", lease_seconds=30)
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    store.conn.execute(
        "UPDATE audit_job SET lease_expires_at=? WHERE id IN (?,?)",
        (expired, retryable.job_id, exhausted.job_id),
    )
    store.conn.commit()
    assert jobs.recover_expired() == 2
    assert jobs.get(retryable.job_id).status == "queued"
    assert jobs.get(exhausted.job_id).status == "failed"
    assert jobs.get(exhausted.job_id).error_code == "JOB_LEASE_EXPIRED"


def test_cancel_requires_current_non_running_job_version(tmp_path) -> None:  # noqa: ANN001
    store = Store(tmp_path / "cancel.db")
    jobs = JobRepository(store)
    queued = jobs.enqueue("fake", {}, created_by="reviewer")

    cancelled = jobs.cancel(queued.job_id, expected_version=queued.state_version)
    assert cancelled.status == "cancelled"
    assert jobs.events_after(0)[-1].event_type == "job.cancelled"
    with pytest.raises(StateConflictError):
        jobs.cancel(queued.job_id, expected_version=queued.state_version)

    running = jobs.enqueue("fake", {}, created_by="reviewer")
    claimed = jobs.claim_next("worker-one", lease_seconds=30)
    assert claimed is not None and claimed.job_id == running.job_id
    with pytest.raises(StateConflictError):
        jobs.cancel(running.job_id, expected_version=claimed.state_version)


def test_single_worker_persists_progress_and_contains_errors(tmp_path) -> None:  # noqa: ANN001
    async def scenario() -> None:
        store = Store(tmp_path / "worker.db")
        jobs = JobRepository(store)

        async def success(context):  # noqa: ANN001, ANN202
            await context.progress(25, "第一步")
            await context.progress(75, "第二步")
            return {"done": True}

        async def fail(_context):  # noqa: ANN001, ANN202
            raise RuntimeError("token=must-not-leak")

        worker = JobWorker(
            jobs,
            {"success": success, "fail": fail},
            poll_seconds=0.02,
            lease_seconds=5,
        )
        good = jobs.enqueue("success", {}, created_by="r")
        bad = jobs.enqueue("fail", {}, created_by="r")
        worker.start()
        for _ in range(100):
            if jobs.get(bad.job_id).status == "failed":
                break
            await asyncio.sleep(0.02)
        await worker.stop()
        assert jobs.get(good.job_id).status == "completed"
        assert jobs.get(good.job_id).result == {"done": True}
        failed = jobs.get(bad.job_id)
        assert failed.status == "failed"
        assert "must-not-leak" not in failed.error_message
        assert "[REDACTED]" in failed.error_message

    asyncio.run(scenario())
