import asyncio
from datetime import timedelta

import pytest

from app.queue.tasks import check_schedule


@pytest.mark.asyncio
async def test_not_due_checks_heartbeat_without_submitting(db):
    assert await check_schedule() is None
    assert await db.fetchval("SELECT count(*) FROM collector.jobs") == 0
    assert await db.fetchval("SELECT interval_minutes FROM collector.schedules") == 30
    assert await db.fetchval("SELECT last_success_at IS NOT NULL FROM collector.scheduler_state")


@pytest.mark.asyncio
async def test_changed_interval_and_duplicate_ticks_are_atomic(db):
    await check_schedule()
    await db.execute("UPDATE collector.schedules SET interval_minutes=75,next_run_at=now()-interval '1 minute'")
    before = await db.fetchval("SELECT clock_timestamp()")
    results = await asyncio.gather(check_schedule(), check_schedule())
    assert sum(result is not None for result in results) == 1
    assert await db.fetchval("SELECT count(*) FROM collector.jobs WHERE mode='refresh'") == 1
    due = await db.fetchval("SELECT next_run_at FROM collector.schedules")
    assert before + timedelta(minutes=75) <= due < before + timedelta(minutes=76)
    # An unfinished automatic refresh is reused rather than queued repeatedly.
    await db.execute("UPDATE collector.schedules SET next_run_at=now()-interval '1 minute'")
    assert await check_schedule() is None
    assert await db.fetchval("SELECT count(*) FROM collector.jobs WHERE mode='refresh'") == 1


@pytest.mark.asyncio
async def test_failed_submission_keeps_due_time_for_retry(db, monkeypatch):
    await check_schedule()
    await db.execute("UPDATE collector.schedules SET next_run_at=now()-interval '1 minute'")
    due = await db.fetchval("SELECT next_run_at FROM collector.schedules")

    async def fail(*args):
        raise RuntimeError('submission failed')

    monkeypatch.setattr('app.queue.tasks.submit', fail)
    with pytest.raises(RuntimeError):
        await check_schedule()
    assert await db.fetchval("SELECT next_run_at FROM collector.schedules") == due
    assert await db.fetchval("SELECT error FROM collector.scheduler_state") == 'SCHEDULE_SUBMIT_FAILED'
    assert await db.fetchval("SELECT count(*) FROM collector.jobs") == 0
