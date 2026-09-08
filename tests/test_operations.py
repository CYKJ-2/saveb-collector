import pytest
from conftest import FakeClient, order
from litestar.testing import AsyncTestClient

from app.config.settings import get_settings
from app.domain.jobs import JobRequest
from app.services.collection import run_chunk
from app.services.jobs import status, submit


@pytest.mark.asyncio
async def test_api_submit_status_and_unknown_id(db):
    from app.main import app

    async with AsyncTestClient(app=app) as client:
        headers = {
            "Authorization": "Bearer test-service-token",
            "Idempotency-Key": "api-click",
            "X-Collector-Actor": "erp:123",
        }
        result = await client.post(
            "/api/collect/jobs",
            json={"mode": "history", "start": "2026-09-01", "end": "2026-09-01"},
            headers=headers,
        )
        assert result.status_code == 202, result.text
        value = result.json()
        assert value["actor"] == "erp:123"
        fetched = await client.get(value["statusUrl"], headers=headers)
        assert fetched.json()["jobId"] == value["jobId"]
        unknown = await client.get("/api/collect/jobs/no-such-id", headers=headers)
        assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_broker_failure_retains_outbox_and_successful_dispatch_still_watchdogs(
    db, monkeypatch
):
    from app.queue.tasks import celery_app, dispatch

    job = await submit(
        db, JobRequest(mode="history", start="2026-09-01", end="2026-09-01"), "test", "broker"
    )

    def fail(*args, **kwargs):
        raise ConnectionError("private credentials")

    monkeypatch.setattr(celery_app, "send_task", fail)
    assert await dispatch() == 0
    assert await db.fetchval("SELECT count(*) FROM collector.outbox") == 1
    sent = []
    monkeypatch.setattr(
        celery_app, "send_task", lambda *args, **kwargs: sent.append((args, kwargs))
    )
    assert await dispatch() == 1
    assert sent[0][0] == ("collector.run_history",)
    assert sent[0][1]["args"] == [job["jobId"]]
    assert await db.fetchval("SELECT count(*) FROM collector.outbox") == 1


@pytest.mark.asyncio
async def test_shadow_coverage_cannot_skip_first_erp_publication(db, monkeypatch):
    from test_pipeline import execute

    monkeypatch.setenv("SAVEB_PUBLISH_API", "false")
    get_settings.cache_clear()
    shadow = await execute(db, [order()], "shadow")
    assert shadow["status"] == "succeeded"
    assert not await db.fetchval("SELECT published FROM collector.coverage")
    assert await db.fetchval("SELECT count(*) FROM orders") == 0
    monkeypatch.setenv("SAVEB_PUBLISH_API", "true")
    get_settings.cache_clear()
    job = await submit(
        db, JobRequest(mode="missing", start="2026-09-01", end="2026-09-01"), "test", "publish"
    )
    assert job["total"] == 1
    await run_chunk(db, job["jobId"], type("C", (FakeClient,), {"rows": [order()]}))
    assert (await status(db, job["jobId"]))["status"] == "succeeded"
    assert await db.fetchval("SELECT count(*) FROM orders") == 1


@pytest.mark.asyncio
async def test_rate_changes_do_not_revalue_old_amounts(db, monkeypatch, tmp_path):
    from test_pipeline import execute

    row = order()
    row["order"]["currency"] = "EUR"
    await execute(db, [row], "rate1")
    amount = await db.fetchval("SELECT amount_usd FROM orders")
    path = tmp_path / "new-rates.json"
    path.write_text('{"rates":{"USD":1,"EUR":0.6}}')
    monkeypatch.setenv("SAVEB_RATES_FILE", str(path))
    get_settings.cache_clear()
    row["order"]["updateTime"] = "26-09-01 11:00"
    job = await execute(db, [row], "rate2")
    assert job["status"] == "succeeded", job
    assert await db.fetchval("SELECT amount_usd FROM orders") == amount


@pytest.mark.asyncio
async def test_unknown_exception_rolls_back_projection(db, monkeypatch):
    from test_pipeline import execute

    async def broken(*args):
        raise RuntimeError("SQL contains private customer data")

    monkeypatch.setattr("app.services.collection.rebuild", broken)
    job = await execute(db, [order()], "rollback")
    assert job["status"] == "retrying"
    assert await db.fetchval("SELECT count(*) FROM orders") == 0
    assert await db.fetchval("SELECT count(*) FROM collector.source_orders") == 0
    assert job["error"] == "COLLECTION_INTERNAL_ERROR"


def test_legacy_image_counter_and_allocation_rules():
    from app.collectors.product_images import image_count
    from app.domain.orders import normalize

    assert (
        image_count('class="lightgallery-product-images" data-images=\'[{"src":"a"},{"src":"b"}]\'')
        == 2
    )
    assert image_count('<img data-largeimg="a"><img data-largeimg="a"><img data-largeimg="b">') == 2
    row = order()
    row["order"]["clientSite"] = "saveb-link.net"
    row["productList"][0]["productName"] = "AB 50 XY 50"
    row["_collector_image_counts"] = [4]
    result = normalize(
        row, {"rules": {"staffCodes": ["AB", "XY"]}, "rates": {"USD": 1}, "version": "test"}
    )
    assert result["columns"]["items_count"] == 4
    assert result["summary"]["staffAllocations"] == [
        {"staff": "AB", "items": 2.0},
        {"staff": "XY", "items": 2.0},
    ]


@pytest.mark.asyncio
async def test_staff_allocation_totals_and_test_orders_excluded(db):
    from test_pipeline import execute

    row = order()
    row["order"]["clientSite"] = "saveb-link.net"
    row["productList"][0]["productName"] = "AB 1 XY 1"
    test_row = order("testing")
    test_row["address"]["customerFirstname"] = "Testing"
    job = await execute(db, [row, test_row], "staff")
    assert job["status"] == "succeeded", job
    assert await db.fetchval(
        "SELECT sum(usd_amount) FROM daily_stats WHERE staff_code<>''"
    ) == await db.fetchval("SELECT usd_amount FROM daily_stats WHERE staff_code=''")
    assert await db.fetchval("SELECT count(*) FROM daily_stats WHERE staff_code<>''") == 2
    assert await db.fetchval("SELECT orders_count FROM daily_stats WHERE staff_code=''") == 1


@pytest.mark.asyncio
async def test_partial_failure_only_retries_failed_chunk(db):
    from app.collectors.dh_order import CollectionError
    from app.services.jobs import resume

    calls = []

    class Mixed(FakeClient):
        async def collect(self, scope):
            calls.append(scope["day"])
            if scope["day"] == "2026-09-02":
                raise CollectionError("AUTH_SESSION_INVALID")
            return [order()]

    job = await submit(
        db, JobRequest(mode="history", start="2026-09-01", end="2026-09-02"), "test", "partial"
    )
    await run_chunk(db, job["jobId"], Mixed)
    await run_chunk(db, job["jobId"], Mixed)
    assert (await status(db, job["jobId"]))["status"] == "partial_failed"
    await resume(db, job["jobId"])
    await run_chunk(db, job["jobId"], type("Good", (FakeClient,), {"rows": []}))
    final = await status(db, job["jobId"])
    assert final["status"] == "succeeded"
    assert final["chunks"][0]["attempts"] == 1
