from datetime import date
from decimal import Decimal

import pytest
from conftest import FakeClient, order

from app.collectors.dh_order import CollectionError
from app.domain.jobs import JobRequest
from app.persistence.db import connect
from app.services.collection import run_chunk
from app.services.jobs import cancel, resume, status, submit


async def execute(db, rows, key, dry=False):
    job = await submit(
        db,
        JobRequest(mode="history", start="2026-09-01", end="2026-09-01", dry_run=dry),
        "test",
        key,
    )
    client = type("Client", (FakeClient,), {"rows": rows, "error": None})
    await run_chunk(db, job["jobId"], client)
    return await status(db, job["jobId"])


@pytest.mark.asyncio
async def test_insert_update_stats_and_idempotent_delivery(db):
    first = await execute(db, [order()], "one")
    assert first["status"] == "succeeded", first
    assert await db.fetchval("SELECT count(*) FROM orders") == 1
    assert await db.fetchval(
        "SELECT usd_amount FROM daily_stats WHERE staff_code='' AND channel='official'"
    ) == Decimal("12.30")
    await run_chunk(db, first["jobId"], FakeClient)
    assert await db.fetchval("SELECT count(*) FROM collector.changes") == 1
    second = await execute(db, [order(amount="15.50", updated="26-09-01 11:00")], "two")
    assert second["status"] == "succeeded", second
    assert await db.fetchval("SELECT amount_original FROM orders") == Decimal("15.50")
    assert await db.fetchval("SELECT count(*) FROM orders") == 1


@pytest.mark.asyncio
async def test_pending_to_completed_and_refund_moves_stats(db):
    assert (await execute(db, [order(status="Pending")], "pending"))["status"] == "succeeded"
    assert await db.fetchval("SELECT count(*) FROM daily_stats") == 0
    assert (await execute(db, [order(updated="26-09-02 11:00")], "complete"))[
        "status"
    ] == "succeeded"
    assert await db.fetchval("SELECT stat_date FROM daily_stats") == date(2026, 9, 1)
    assert (await execute(db, [order(status="Refunded", updated="26-09-02 12:00")], "refund"))[
        "status"
    ] == "succeeded"
    assert await db.fetchval("SELECT count(*) FROM daily_stats") == 0
    assert await db.fetchval("SELECT count(*) FROM orders") == 1


@pytest.mark.asyncio
async def test_manual_field_and_raw_overrides_survive(db):
    await execute(db, [order()], "a")
    await db.execute("UPDATE orders SET staff_code='MANUAL',customer_name='Manual name'")
    await execute(db, [order(amount="20", updated="26-09-01 11:00")], "b")
    row = await db.fetchrow("SELECT * FROM orders")
    assert row["staff_code"] == "MANUAL"
    assert row["customer_name"] == "Manual name"
    assert row["amount_original"] == 20
    await execute(db, [order(amount="21", updated="26-09-01 12:00")], "c")
    assert await db.fetchval("SELECT staff_code FROM orders") == "MANUAL"


@pytest.mark.asyncio
async def test_stale_does_not_overwrite(db):
    await execute(db, [order(amount="20", updated="26-09-02 10:00")], "new")
    job = await execute(db, [order(amount="10", updated="26-09-01 10:00")], "old")
    assert job["chunks"][0]["counts"]["stale"] == 1
    assert await db.fetchval("SELECT amount_original FROM orders") == 20


@pytest.mark.asyncio
async def test_same_source_version_conflict_fails_atomically(db):
    await execute(db, [order()], "initial")
    job = await execute(db, [order(key="B"), order(amount="55")], "conflict")
    assert job["status"] == "failed"
    assert await db.fetchval("SELECT count(*) FROM orders") == 1
    assert await db.fetchval("SELECT amount_original FROM orders") == Decimal("12.30")


@pytest.mark.asyncio
async def test_remark_changes_without_timestamp_do_not_block_collection(db):
    original = order()
    original["order"]["remark"] = "first annotation"
    await execute(db, [original], "remark-initial")
    await db.execute("UPDATE orders SET staff_code='MANUAL'")
    changed = order()
    changed["order"]["remark"] = "corrected annotation"
    job = await execute(db, [changed, order(key="B")], "remark-update")
    assert job["status"] == "succeeded", job
    assert await db.fetchval("SELECT raw->'order'->>'remark' FROM collector.source_orders WHERE order_id='A'") == "corrected annotation"
    assert await db.fetchval("SELECT count(*) FROM orders") == 2
    assert await db.fetchval("SELECT staff_code FROM orders WHERE order_id='A'") == "MANUAL"
    assert await db.fetchval("SELECT amount_original FROM orders WHERE order_id='A'") == Decimal("12.30")
    assert job["chunks"][0]["counts"]["updated"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("amount", "55"), ("paymentStatus", "Refunded")])
async def test_remark_exception_does_not_hide_business_field_conflicts(db, field, value):
    await execute(db, [order()], "baseline")
    changed = order()
    changed["order"].update(remark="annotation", **{field: value})
    job = await execute(db, [order(key="B"), changed], "material-conflict")
    assert job["status"] == "failed"
    assert job["chunks"][0]["error"] == "SOURCE_VERSION_CONFLICT"
    assert await db.fetchval("SELECT count(*) FROM orders") == 1
    assert await db.fetchval("SELECT amount_original FROM orders") == Decimal("12.30")
    assert await db.fetchval("SELECT order_status FROM orders") == "completed"


@pytest.mark.asyncio
async def test_dry_run_archives_without_publication(db):
    job = await execute(db, [order()], "dry", dry=True)
    assert job["status"] == "succeeded", job
    assert await db.fetchval("SELECT count(*) FROM orders") == 0
    assert await db.fetchval("SELECT count(*) FROM collector.source_orders") == 0
    assert await db.fetchval("SELECT count(*) FROM collector.coverage") == 0
    assert await db.fetchval("SELECT raw IS NOT NULL FROM collector.chunks")


@pytest.mark.asyncio
async def test_image_migration_updates_summary_and_preserves_manual_columns(db):
    original = order()
    original["productList"][0]["productImg"] = "https://www.example/old.jpg"
    await execute(db, [original], "image-before")
    await db.execute("UPDATE orders SET staff_code='MANUAL'")
    changed = order()
    changed["productList"][0]["productImg"] = "https://img.example/new.jpg"
    job = await execute(db, [changed, order(key="B")], "image-after")
    assert job["status"] == "succeeded", job
    assert await db.fetchval("SELECT count(*) FROM orders") == 2
    assert await db.fetchval("SELECT staff_code FROM orders WHERE order_id='A'") == "MANUAL"
    assert await db.fetchval(
        "SELECT raw->'products'->0->>'image' FROM orders WHERE order_id='A'"
    ) == "https://img.example/new.jpg"
    assert await db.fetchval(
        "SELECT raw->'productList'->0->>'productImg' FROM collector.source_orders WHERE order_id='A'"
    ) == "https://img.example/new.jpg"


@pytest.mark.asyncio
async def test_idempotency_outbox_and_missing(db):
    request = JobRequest(mode="history", start="2026-09-01", end="2026-09-01")
    a = await submit(db, request, "test", "same")
    b = await submit(db, request, "test", "same")
    assert a["jobId"] == b["jobId"]
    assert await db.fetchval("SELECT count(*) FROM collector.outbox") == 1
    with pytest.raises(ValueError, match="CONFLICT"):
        await submit(db, request.model_copy(update={"dry_run": True}), "test", "same")
    await run_chunk(db, a["jobId"], type("Empty", (FakeClient,), {"rows": []}))
    missing = await submit(db, request.model_copy(update={"mode": "missing"}), "test", "missing")
    assert missing["status"] == "succeeded" and missing["total"] == 0


@pytest.mark.asyncio
async def test_failed_auth_retains_data_and_can_resume(db):
    await execute(db, [order()], "baseline")
    request = JobRequest(mode="history", start="2026-09-01", end="2026-09-01")
    job = await submit(db, request, "test", "auth")
    client = type("Bad", (FakeClient,), {"error": CollectionError("AUTH_SESSION_INVALID")})
    await run_chunk(db, job["jobId"], client)
    assert (await status(db, job["jobId"]))["status"] == "failed"
    assert await db.fetchval("SELECT count(*) FROM orders") == 1
    await resume(db, job["jobId"])
    await run_chunk(db, job["jobId"], type("Good", (FakeClient,), {"rows": [order()]}))
    assert (await status(db, job["jobId"]))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_lock_prevents_concurrent_publication_and_cancel(db):
    job = await submit(
        db, JobRequest(mode="history", start="2026-09-01", end="2026-09-01"), "test", "lock"
    )
    await db.execute("SELECT pg_advisory_lock(hashtextextended('collector-source:default',0))")
    try:
        async with connect() as other:
            await run_chunk(other, job["jobId"], FakeClient)
        assert (await status(db, job["jobId"]))["status"] == "queued"
    finally:
        await db.execute(
            "SELECT pg_advisory_unlock(hashtextextended('collector-source:default',0))"
        )
    await cancel(db, job["jobId"])
    await run_chunk(db, job["jobId"], FakeClient)
    assert (await status(db, job["jobId"]))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_reprocess_uses_evidence_and_cannot_publish(db):
    original = await execute(db, [order()], "raw", dry=True)
    request = JobRequest(
        mode="reprocess", start="2026-09-01", end="2026-09-01", source_job_id=original["jobId"]
    )
    assert request.dry_run
    job = await submit(db, request, "test", "replay")
    await run_chunk(db, job["jobId"])
    assert (await status(db, job["jobId"]))["status"] == "succeeded"
    assert await db.fetchval("SELECT count(*) FROM orders") == 0


@pytest.mark.asyncio
async def test_unknown_currency_does_not_become_zero(db):
    row = order()
    row["order"]["currency"] = "XYZ"
    job = await execute(db, [row], "invalid")
    assert job["status"] == "failed"
    assert await db.fetchval("SELECT count(*) FROM orders") == 0
    assert job["chunks"][0]["error"] == "CURRENCY_RATE_MISSING"
