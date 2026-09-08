import asyncio
from datetime import datetime
from decimal import Decimal

import pytest
from conftest import FakeClient, order
from test_pipeline import execute

from app.domain.jobs import JobRequest
from app.domain.orders import SHANGHAI
from app.services.collection import run_chunk
from app.services.jobs import status, submit


@pytest.mark.asyncio
async def test_today_has_only_one_scope_and_does_not_collect_history(db):
    today = datetime.now(SHANGHAI)
    stamp = today.strftime("%y-%m-%d 10:00")
    job = await submit(db, JobRequest(mode="today"), "saveb-api:1", "today")
    assert [c["scope"] for c in job["chunks"]] == [{"day": today.date().isoformat()}]
    client = type(
        "TodayClient",
        (FakeClient,),
        {
            "rows": [
                order("today", day=stamp, updated=stamp),
                order("old", day="21-01-01 10:00", updated="21-01-01 10:00"),
            ]
        },
    )
    await run_chunk(db, job["jobId"], client)
    result = await status(db, job["jobId"])
    assert result["status"] == "succeeded", result
    assert result["publication"] == "saveb-api"
    assert await db.fetchval("SELECT count(*) FROM orders") == 1
    assert await db.fetchval("SELECT order_id FROM orders") == "today"
    assert await db.fetchval("SELECT count(*) FROM collector.checkpoints") == 0


@pytest.mark.asyncio
async def test_api_identity_version_and_soft_delete_are_preserved(db):
    await execute(db, [order()], "first")
    before = await db.fetchrow("SELECT entity_uuid,visible_order_id,version FROM orders")
    assert before["entity_uuid"] and before["visible_order_id"]
    await execute(db, [order(amount="20", updated="26-09-01 11:00")], "update")
    after = await db.fetchrow("SELECT entity_uuid,visible_order_id,version FROM orders")
    assert after["entity_uuid"] == before["entity_uuid"]
    assert after["visible_order_id"] == before["visible_order_id"]
    assert after["version"] == before["version"] + 1
    await db.execute("UPDATE orders SET deleted_at=now()")
    await execute(db, [order(amount="30", updated="26-09-01 12:00")], "deleted")
    assert await db.fetchval("SELECT amount_original FROM orders") == 20
    assert await db.fetchval("SELECT count(*) FROM daily_stats") == 0


@pytest.mark.asyncio
async def test_scheduler_submission_failure_has_persisted_heartbeat(db, monkeypatch):
    from app.queue import tasks

    async def fail(*args):
        raise ValueError("simulated failure")

    monkeypatch.setattr(tasks, "submit", fail)
    await db.execute("INSERT INTO collector.schedules(account,next_run_at) VALUES('default',now()-interval '1 minute')")
    with pytest.raises(ValueError):
        await asyncio.to_thread(tasks.schedule_task.run)
    heartbeat = await db.fetchrow("SELECT * FROM collector.scheduler_state")
    assert heartbeat["last_attempt_at"]
    assert heartbeat["last_success_at"] is None
    assert heartbeat["error"] == "SCHEDULE_SUBMIT_FAILED"


@pytest.mark.asyncio
async def test_rates_are_inverted_for_api_table(db, monkeypatch):
    from app.collectors import exchange_rates

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"base": "USD", "date": "2026-09-07", "rates": {"EUR": 0.8}}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            return Response()

    monkeypatch.setattr(exchange_rates.httpx, "AsyncClient", Client)
    await db.execute("DELETE FROM exchange_rates WHERE effective_date='2026-09-07'")
    await exchange_rates.fetch_rates()
    assert await db.fetchval(
        "SELECT rate_to_usd FROM exchange_rates WHERE currency='EUR' AND effective_date='2026-09-07'"
    ) == Decimal("1.25")
