"""滚动窗口、旧队列升级和在途取消只在隔离测试库验证。"""
from datetime import date, datetime

import pytest
from conftest import FakeClient, order

from app.config.settings import get_settings
from app.domain.jobs import JobRequest
from app.services import jobs, pending
from app.services.collection import run_chunk


class FrozenClock(datetime):
    day = date(2026, 9, 14)

    @classmethod
    def now(cls, tz=None):
        return cls(cls.day.year, cls.day.month, cls.day.day, 12, tzinfo=tz)


@pytest.fixture(autouse=True)
def recent_window(config, monkeypatch):
    monkeypatch.setenv("SAVEB_HISTORY_START", "2021-08-10")
    monkeypatch.setenv("SAVEB_PENDING_LOOKBACK_DAYS", "30")
    monkeypatch.setenv("SAVEB_PENDING_ENABLED", "true")
    monkeypatch.setenv("SAVEB_PENDING_WINDOW_DAYS", "7")
    monkeypatch.setattr(FrozenClock, "day", date(2026, 9, 14))
    monkeypatch.setattr(pending, "datetime", FrozenClock)
    monkeypatch.setattr(jobs, "datetime", FrozenClock)
    get_settings.cache_clear()


def test_rolling_calendar_days_and_later_history_start(monkeypatch):
    assert pending.pending_bounds() == (date(2026, 8, 16), date(2026, 9, 14))
    assert pending.pending_bounds(date(2026, 3, 1))[0] == date(2026, 1, 31)
    assert pending.pending_bounds(date(2028, 3, 1))[0] == date(2028, 2, 1)
    monkeypatch.setenv("SAVEB_HISTORY_START", "2026-09-01")
    get_settings.cache_clear()
    assert pending.pending_bounds()[0] == date(2026, 9, 1)


async def old_automatic(db):
    return (await jobs.submit(db, JobRequest(mode="pending", start="2022-09-13", end="2022-09-19"),
                              "pending-scheduler", "old-auto"))["jobId"]


async def test_upgrade_cancels_old_queue_and_moves_cursor_into_recent_window(db):
    old_id = await old_automatic(db)
    await db.execute("INSERT INTO collector.pending_state(account,cursor_day) VALUES($1,'2022-09-13')",
                     get_settings().source_account)
    new_id = await pending.schedule_pending(db)
    assert new_id != old_id
    assert (await jobs.status(db, old_id))["cancel_requested"]
    new = await jobs.status(db, new_id)
    assert new["params"]["start"] == "2026-08-16"
    assert new["params"]["end"] == "2026-08-22"
    await run_chunk(db, old_id, NeverFetch)
    assert (await jobs.status(db, old_id))["status"] == "cancelled"
    assert await db.fetchval("SELECT count(*) FROM collector.outbox WHERE job_id=$1", old_id) == 0


class NeverFetch(FakeClient):
    async def collect(self, scope):
        pytest.fail("expired queued work must not call upstream")


async def test_worker_rejects_old_queue_even_before_beat_runs(db):
    old_id = await old_automatic(db)
    await run_chunk(db, old_id, NeverFetch)
    job = await jobs.status(db, old_id)
    assert job["status"] == "cancelled"
    assert job["error"] == "PENDING_WINDOW_EXPIRED"
    assert await db.fetchval("SELECT count(*) FROM collector.pending_coverage") == 0


async def test_full_cycle_wraps_to_recent_lower_bound_and_slides(db, monkeypatch):
    await db.execute("INSERT INTO collector.pending_state(account,cursor_day) VALUES($1,'2026-09-13')",
                     get_settings().source_account)
    job_id = await pending.schedule_pending(db)
    await run_chunk(db, job_id, FakeClient)
    state = await db.fetchrow("SELECT * FROM collector.pending_state")
    assert state["cursor_day"] == date(2026, 8, 16)
    assert state["cycles_completed"] == 1
    monkeypatch.setattr(FrozenClock, "day", date(2026, 9, 15))
    await db.execute("UPDATE collector.pending_state SET next_run_at=now()")
    next_id = await pending.schedule_pending(db)
    assert (await jobs.status(db, next_id))["params"]["start"] == "2026-08-17"


async def test_manual_older_range_is_still_one_off(db):
    job = await jobs.submit(db, JobRequest(mode="pending", start="2022-09-13", end="2022-09-19"),
                            "operator", "manual-old")
    await pending.schedule_pending(db)
    await run_chunk(db, job["jobId"], FakeClient)
    result = await jobs.status(db, job["jobId"])
    assert result["status"] == "succeeded"
    assert not result["cancel_requested"]
    assert await db.fetchval("SELECT cycles_completed FROM collector.pending_state") == 0


async def test_padding_cannot_publish_orders_older_than_window(db):
    job_id = await pending.schedule_pending(db)
    client = type("Rows", (FakeClient,), {"rows": [
        order("old-padding", status="Pending", day="26-08-15 12:00"),
        order("within", status="Pending", day="26-08-16 12:00"),
    ]})
    await run_chunk(db, job_id, client)
    assert (await jobs.status(db, job_id))["status"] == "succeeded"
    assert await db.fetchval("SELECT count(*) FROM orders") == 1
    assert await db.fetchval("SELECT count(*) FROM collector.source_orders WHERE order_id='old-padding'") == 0


async def test_inflight_midnight_expiry_does_not_publish_response(db, monkeypatch):
    job_id = await pending.schedule_pending(db)

    class Midnight(FakeClient):
        async def collect(self, scope):
            monkeypatch.setattr(FrozenClock, "day", date(2026, 9, 15))
            return [order("inflight", status="Pending", day="26-08-17 12:00")]

    await run_chunk(db, job_id, Midnight)
    assert (await jobs.status(db, job_id))["status"] == "cancelled"
    assert await db.fetchval("SELECT count(*) FROM orders") == 0
    assert await db.fetchval("SELECT count(*) FROM collector.pending_coverage") == 0


async def test_expiry_preserves_already_committed_orders_and_chunks(db, monkeypatch):
    job = await jobs.submit(db, JobRequest(mode="pending", start="2026-08-16", end="2026-08-29"),
                            "pending-scheduler", "partially-done")
    client = type("Rows", (FakeClient,), {
        "rows": [order("committed", status="Pending", day="26-08-16 12:00")],
    })
    await run_chunk(db, job["jobId"], client)
    monkeypatch.setattr(FrozenClock, "day", date(2026, 9, 15))
    await run_chunk(db, job["jobId"], NeverFetch)
    result = await jobs.status(db, job["jobId"])
    assert result["status"] == "cancelled"
    assert [c["status"] for c in result["chunks"]] == ["succeeded", "cancelled"]
    assert await db.fetchval("SELECT count(*) FROM orders") == 1
    assert await db.fetchval("SELECT count(*) FROM collector.pending_coverage") == 1
