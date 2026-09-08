from datetime import UTC, datetime

import httpx
import pytest

from app.collectors.logistics import LogisticsError, parse_kuaidi, provider_name, query_tracking
from app.config.settings import get_settings
from app.services.logistics import LogisticsRequest, job_status, run_chunk, schedule, submit


@pytest.fixture
def logistics_config(monkeypatch):
    monkeypatch.setenv("SAVEB_AFTERSHIP_API_KEY", "test-aftership")
    monkeypatch.setenv("SAVEB_KUAIDI100_API_KEY", "test-kuaidi")
    get_settings.cache_clear()


async def task(db, number="TEST123"):
    return await db.fetchval("INSERT INTO procurement_tasks(order_id,purchase_status,tracking_no,raw) VALUES('ORDER','pending_purchase',$1,$2) RETURNING id",
                             number, {"trackingCarrier": "顺丰", "trackingPhone": "1234", "notes": "manual"})


async def delivered(*args):
    return {"status": "delivered", "checkpoint": "Delivered", "occurred_at": "2026-09-08T10:00:00+08:00", "carrier": "sf-express", "provider_id": "test-id", "provider_status": "Delivered", "checked_at": datetime.now(UTC).isoformat()}


async def test_delivery_persists_event_preserves_business_and_is_idempotent(db, logistics_config):
    target = await task(db)
    request = LogisticsRequest(taskId=target)
    job = await submit(db, request, "operator", "same")
    assert (await submit(db, request, "operator", "same"))["jobId"] == job["jobId"]
    await run_chunk(db, job["jobId"], delivered)
    assert (await job_status(db, job["jobId"]))["status"] == "succeeded"
    row = await db.fetchrow("SELECT * FROM procurement_tasks WHERE id=$1", target)
    assert row["purchase_status"] == "pending_purchase"
    assert row["raw"]["notes"] == "manual"
    assert row["raw"]["deliveryStatus"] == "delivered"
    assert await db.fetchval("SELECT count(*) FROM shipment_tracking_events") == 1
    second = await submit(db, request, "operator", "second")
    await run_chunk(db, second["jobId"], delivered)
    assert await db.fetchval("SELECT count(*) FROM shipment_tracking_events") == 1


async def test_changed_tracking_skips_old_result(db, logistics_config):
    target = await task(db)
    job = await submit(db, LogisticsRequest(taskId=target), "operator", "changed")
    async def query(*args):
        await db.execute("UPDATE procurement_tasks SET tracking_no='NEW-NUMBER',raw=raw||'{\"supplierNote\":\"keep\"}'::jsonb WHERE id=$1", target)
        return await delivered()
    await run_chunk(db, job["jobId"], query)
    result = await job_status(db, job["jobId"])
    assert result["skipped"] == 1
    assert await db.fetchval("SELECT count(*) FROM shipment_tracking_events") == 0
    assert await db.fetchval("SELECT raw->>'supplierNote' FROM procurement_tasks WHERE id=$1", target) == "keep"


async def test_failure_retry_exhaustion_and_scheduler_skip_delivered(db, logistics_config):
    target = await task(db)
    job = await submit(db, LogisticsRequest(taskId=target), "operator", "failed")
    async def query(*args):
        raise LogisticsError("PROVIDER_QUERY_FAILED")
    for _ in range(3):
        await run_chunk(db, job["jobId"], query)
    assert (await job_status(db, job["jobId"]))["status"] == "failed"
    await db.execute("UPDATE procurement_tasks SET raw=raw||'{\"deliveryStatus\":\"delivered\",\"trackingQueriedNumber\":\"TEST123\"}'::jsonb WHERE id=$1", target)
    await schedule(db)
    assert await db.fetchval("SELECT count(*) FROM collector.chunks c JOIN collector.jobs j ON j.id=c.job_id WHERE actor='logistics-scheduler'") == 0
    assert await db.fetchval("SELECT count(*) FROM collector.logistics_schedule") == 1


async def test_keys_missing_and_source_collection_independent(db, monkeypatch):
    monkeypatch.setenv("SAVEB_AFTERSHIP_API_KEY", "")
    monkeypatch.setenv("SAVEB_KUAIDI100_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(LogisticsError, match="NOT_CONFIGURED"):
        await submit(db, LogisticsRequest(), "operator", "missing")
    await schedule(db)
    assert await db.fetchval("SELECT count(*) FROM collector.jobs") == 0


async def test_aftership_duplicate_registration_lookup(logistics_config):
    def respond(request):
        assert request.headers["as-api-key"] == "test-aftership"
        if request.method == "POST":
            return httpx.Response(400, json={"meta": {"code": 4003}})
        return httpx.Response(200, json={"data": {"trackings": [{"id": "exists", "tracking_number": "TEST123", "tag": "Delivered", "checkpoints": []}]}})
    result = await query_tracking("aftership", {"number": "TEST123"}, httpx.MockTransport(respond))
    assert result["status"] == "delivered" and result["provider_id"] == "exists"


def test_kuaidi_schema_mismatch_and_status():
    text = "**快递单号**：TEST123\n**物流状态**：签收\n| 2026-09-08 10:00:00 | 已签收 |"
    assert parse_kuaidi(text, "TEST123")["status"] == "delivered"
    with pytest.raises(LogisticsError, match="MISMATCH"):
        parse_kuaidi(text, "OTHER")
    with pytest.raises(LogisticsError, match="NO_TRACKING_INFORMATION"):
        parse_kuaidi("service unavailable", "TEST123")


def test_provider_priority_and_fallback(logistics_config, monkeypatch):
    assert provider_name() == "aftership"
    monkeypatch.setenv("SAVEB_AFTERSHIP_API_KEY", "")
    get_settings.cache_clear()
    assert provider_name() == "kuaidi100"
