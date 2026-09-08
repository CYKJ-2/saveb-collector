from datetime import date

import pytest
from conftest import FakeClient, order
from test_pipeline import execute

from app.collectors.dh_order import CollectionError, DHOrderClient
from app.domain.jobs import JobRequest
from app.services.collection import run_chunk
from app.services.jobs import resume, status, submit
from app.services.pending import schedule_pending
from app.services.reconciliation import reconcile


async def test_pending_protocol_and_budget():
    async with DHOrderClient() as client:
        seen = []
        async def page(params):
            seen.append(params)
            return 0, []
        client.page = page
        await client.collect({"pending_start": "2026-09-01", "pending_end": "2026-09-07"})
        assert seen[0]["paymentStatus"] == "Pending"
        assert seen[0]["startDate"].strip() == "2026-08-31"
        assert seen[0]["endDate"].strip() == "2026-09-08"
        async def too_many(params):
            return 1001, []
        client.page = too_many
        with pytest.raises(CollectionError, match="PAGE_BUDGET"):
            await client.collect({"pending_start": "2026-09-01", "pending_end": "2026-09-07"})


async def test_reconcile_selected_sql(db):
    report = await reconcile(db, date(2026, 9, 1), date(2026, 9, 1), order_ids=["unknown"])
    assert report["unarchived_selected"] == 1
    assert report["status"] == "warning"


async def test_pending_budget_splits_without_skipping_failed_window(db):
    job_id = await schedule_pending(db)
    budget = type("Budget", (FakeClient,), {"error": CollectionError("PAGE_BUDGET_EXCEEDED")})
    empty = type("Empty", (FakeClient,), {"rows": []})
    await run_chunk(db, job_id, budget)
    job = await status(db, job_id)
    assert [c["status"] for c in job["chunks"]] == ["split", "queued", "queued"]
    await run_chunk(db, job_id, empty)
    assert await db.fetchval("SELECT cursor_day FROM collector.pending_state") == date(2026, 9, 1)
    await run_chunk(db, job_id, empty)
    assert (await status(db, job_id))["status"] == "succeeded"
    assert await db.fetchval("SELECT cursor_day FROM collector.pending_state") == date(2026, 9, 8)


async def test_pending_discovery_does_not_claim_full_date_coverage(db):
    job_id = await schedule_pending(db)
    assert job_id
    assert await schedule_pending(db) is None
    client = type("Pending", (FakeClient,), {"rows": [order("unknown", status="Pending"), order("unfiltered-completed")]})
    await run_chunk(db, job_id, client)
    assert (await status(db, job_id))["status"] == "succeeded"
    assert await db.fetchval("SELECT count(*) FROM orders") == 1
    assert await db.fetchval("SELECT count(*) FROM collector.pending_coverage") == 1
    assert await db.fetchval("SELECT count(*) FROM collector.coverage") == 0
    assert await db.fetchval("SELECT cursor_day FROM collector.pending_state") == date(2026, 9, 8)
    assert await db.fetchval("SELECT status FROM collector.reconciliation_reports") == "passed"


async def test_failed_pending_does_not_advance_and_resume_works(db):
    job_id = await schedule_pending(db)
    bad = type("Bad", (FakeClient,), {"error": CollectionError("AUTH_SESSION_INVALID")})
    await run_chunk(db, job_id, bad)
    assert await db.fetchval("SELECT cursor_day FROM collector.pending_state") == date(2026, 9, 1)
    assert await db.fetchval("SELECT count(*) FROM collector.pending_coverage") == 0
    await resume(db, job_id)
    await run_chunk(db, job_id, type("Empty", (FakeClient,), {"rows": []}))
    assert (await status(db, job_id))["status"] == "succeeded"
    assert await db.fetchval("SELECT cursor_day FROM collector.pending_state") == date(2026, 9, 8)


async def test_pending_preview_and_manual_scan_leave_cursor_unchanged(db):
    for dry in (True, False):
        job = await submit(db, JobRequest(mode="pending", start="2026-09-01", end="2026-09-08", dry_run=dry), "test", str(dry))
        assert job["total"] == 2
        await run_chunk(db, job["jobId"], type("Empty", (FakeClient,), {"rows": []}))
        await run_chunk(db, job["jobId"], type("Empty", (FakeClient,), {"rows": []}))
        if dry:
            assert await db.fetchval("SELECT count(*) FROM collector.pending_coverage") == 0
    assert await db.fetchval("SELECT count(*) FROM collector.pending_state") == 0


async def test_reconcile_detects_stats_snapshot_and_missing_order_without_repair(db):
    await execute(db, [order()], "base")
    day = date(2026, 9, 1)
    assert (await reconcile(db, day, day))["status"] == "passed"
    await db.execute("UPDATE daily_stats SET usd_amount=999 WHERE staff_code=''")
    await db.execute("UPDATE legacy_dashboard_days SET payload=jsonb_set(payload,'{totals,orders}','9')")
    report = await reconcile(db, day, day)
    assert report["status"] == "warning"
    assert report["stats_difference_count"] == 1
    assert report["snapshot_difference_count"] == 1
    assert await db.fetchval("SELECT usd_amount FROM daily_stats") == 999
    await db.execute("DELETE FROM orders")
    assert (await reconcile(db, day, day))["source"]["missing_orders"] == 1


async def test_manual_fields_and_soft_delete_are_not_source_errors(db):
    await execute(db, [order()], "base")
    day = date(2026, 9, 1)
    baseline = await reconcile(db, day, day)
    assert baseline["source"]["manually_adjusted"] == 0
    await db.execute("UPDATE orders SET customer_name='Manual'")
    report = await reconcile(db, day, day)
    assert report["status"] == "passed"
    assert report["source"]["manually_adjusted"] == 1
    await db.execute("UPDATE orders SET deleted_at=now()")
    from app.persistence.projections import rebuild
    await rebuild(db, {day}, "manual-delete")
    report = await reconcile(db, day, day)
    assert report["status"] == "passed"
    assert report["source"]["soft_deleted"] == 1
