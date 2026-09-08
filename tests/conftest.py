import json
import os
from pathlib import Path

import pytest
import pytest_asyncio

from app.config.settings import get_settings
from app.persistence.db import connect


@pytest.fixture(autouse=True)
def config(monkeypatch, tmp_path):
    monkeypatch.setenv("SAVEB_DH_USERNAME", "")
    monkeypatch.setenv("SAVEB_DH_PASSWORD", "")
    monkeypatch.setenv("SAVEB_DH_COOKIE", "test_cookie=value")
    monkeypatch.setenv("SAVEB_APP_ENV", "test")
    monkeypatch.setenv("SAVEB_API_TOKEN", "test-service-token")
    monkeypatch.setenv("SAVEB_DH_BASE_URL", "https://www.dh-order.com")
    monkeypatch.setenv("SAVEB_REQUEST_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("SAVEB_PUBLISH_API", "true")
    monkeypatch.setenv("SAVEB_HISTORY_START", "2026-09-01")
    rules = tmp_path / "rules.json"
    rules.write_text(
        json.dumps({"domainMap": {"saveb-co.com": "Official Sites"}, "staffCodes": ["AB", "XY"]})
    )
    rates = tmp_path / "rates.json"
    rates.write_text(json.dumps({"rates": {"USD": 1, "EUR": 0.8}}))
    monkeypatch.setenv("SAVEB_RULES_FILE", str(rules))
    monkeypatch.setenv("SAVEB_RATES_FILE", str(rates))
    if os.environ.get("COLLECTOR_TEST_DATABASE_URL"):
        monkeypatch.setenv("SAVEB_DATABASE_URL", os.environ["COLLECTOR_TEST_DATABASE_URL"])
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def db(config):
    if not os.environ.get("COLLECTOR_TEST_DATABASE_URL"):
        pytest.skip(
            "Set COLLECTOR_TEST_DATABASE_URL to an isolated database with ERP migrations applied"
        )
    async with connect() as conn:
        # Refuse accidental tests against a business database.
        if await conn.fetchval("SELECT current_database()") != "collector_test":
            raise RuntimeError("integration tests require database named collector_test")
        for migration in sorted((Path(__file__).parents[1] / "migrations").glob("*.sql")):
            await conn.execute(migration.read_text(encoding="utf-8-sig"))
        await conn.execute("TRUNCATE collector.schedules")
        await conn.execute("TRUNCATE collector.pending_state,collector.reconciliation_reports,collector.order_date_repairs")
        await conn.execute("TRUNCATE collector.logistics_schedule")
        await conn.execute(
            "TRUNCATE collector.scheduler_state,collector.changes,collector.source_orders,collector.coverage,collector.checkpoints,collector.outbox,collector.chunks,collector.jobs CASCADE"
        )
        await conn.execute("TRUNCATE orders,daily_stats,legacy_dashboard_days,system_state CASCADE")
        yield conn


def order(
    key="A", status="Completed", amount="12.30", updated="26-09-01 10:00", day="26-09-01 09:00"
):
    return {
        "order": {
            "orderId": key,
            "clientOrderId": "client-" + key,
            "createTime": day,
            "updateTime": updated,
            "amount": amount,
            "currency": "USD",
            "paymentStatus": status,
            "clientSite": "saveb-co.com",
            "recipientAccount": "test@example.invalid",
        },
        "address": {"customerFirstname": "Demo", "customerLastname": "Customer"},
        "productList": [{"productName": "Product", "number": 2, "productId": "p1"}],
    }


class FakeClient:
    rows = ()
    error = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def collect(self, scope):
        if self.error:
            raise self.error
        return self.rows
