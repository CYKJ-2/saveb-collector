import json

import httpx
import pytest
from conftest import order


def test_old_placeholder_host_rejected_before_sending_cookie():
    from app.config.settings import Settings

    with pytest.raises(ValueError, match="dhgate configuration") as error:
        Settings(_env_file=None, dh_base_url="https://www.dhgate.com", dh_cookie="private-cookie")
    assert "private-cookie" not in str(error.value)


def test_production_cannot_use_mock_http_host():
    from app.config.settings import Settings

    with pytest.raises(ValueError):
        Settings(_env_file=None, app_env="production", dh_base_url="http://127.0.0.1:8000")


from app.collectors.dh_order import CollectionError, DHOrderClient


@pytest.mark.asyncio
async def test_protocol_full_cookie_post_and_pagination():
    seen = []

    def handler(request):
        assert request.method == "POST" and request.url.path == "/order/list2"
        assert request.headers["cookie"] == "test_cookie=value"
        params = json.loads(request.content)
        seen.append(params)
        rows = [order(str(i)) for i in (range(100) if params["pageNum"] == 1 else range(100, 101))]
        return httpx.Response(200, json={"code": 1, "totalElement": 101, "data": rows})

    async with DHOrderClient(httpx.MockTransport(handler)) as client:
        rows = await client.collect({"day": "2026-09-01"})
    assert len(rows) == 101 and len(seen) == 2
    assert seen[0]["startDate"] == "2026-08-31 "


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,code",
    [
        (
            httpx.Response(200, json={"code": 0, "totalElement": 0, "data": []}),
            "UPSTREAM_REJECTED_REQUEST",
        ),
        (httpx.Response(302, headers={"location": "/login"}), "AUTH_SESSION_INVALID"),
        (httpx.Response(200, text="<html>Login</html>"), "UPSTREAM_NOT_JSON"),
        (httpx.Response(200, json={"code": 1, "data": []}), "UPSTREAM_TOTAL_MISSING"),
        (
            httpx.Response(200, json={"code": 1, "totalElement": 2, "data": [order()]}),
            "PAGINATION_INCOMPLETE",
        ),
        (
            httpx.Response(200, json={"code": 1, "totalElement": 2, "data": [order(), order()]}),
            "DUPLICATE_ORDER_ID",
        ),
    ],
)
async def test_upstream_failure_is_not_empty_success(response, code):
    async with DHOrderClient(httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(CollectionError, match=code):
            await client.collect({"day": "2026-09-01"})


def test_beat_dynamic_schedule_and_maintenance_queue():
    from app.queue.celery_app import celery_app

    celery_app.loader.import_default_modules()
    assert celery_app.conf.beat_schedule["check-collection-schedule"]["schedule"] == 60.0
    assert celery_app.conf.beat_schedule["discover-historical-pending"]["schedule"] == 60.0
    schedule = celery_app.conf.beat_schedule["daily-reconciliation"]["schedule"]
    assert schedule.hour == {3} and schedule.minute == {10}
    assert {
        "collector.run",
        "collector.run_history",
        "collector.schedule",
        "collector.dispatch",
        "collector.pending_schedule",
        "collector.reconcile",
    } <= set(celery_app.tasks)
    assert celery_app.conf.task_reject_on_worker_lost
    assert celery_app.conf.task_routes["collector.reconcile"]["queue"] == "maintenance"
    assert celery_app.conf.task_routes["collector.pending_schedule"]["queue"] == "maintenance"


@pytest.mark.asyncio
async def test_verified_null_empty_date_response_is_supported_but_missing_fields_fail():
    async with DHOrderClient(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "code": "1",
                    "msg": "Success",
                    "data": None,
                    "totalElement": None,
                },
            )
        )
    ) as client:
        assert await client.collect({"day": "2026-09-07"}) == []
    async with DHOrderClient(
        httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "code": "1",
                    "msg": "Success",
                },
            )
        )
    ) as client:
        with pytest.raises(CollectionError, match="UPSTREAM_SCHEMA_INVALID"):
            await client.collect({"day": "2026-09-07"})


def test_api_requires_auth_and_invalid_request_rejected():
    from litestar.testing import TestClient

    from app.main import app

    with TestClient(app=app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/collect/jobs/missing").status_code == 401
        result = client.post(
            "/api/collect/jobs",
            json={"mode": "history"},
            headers={"Authorization": "Bearer test-service-token"},
        )
        assert result.status_code == 400
        assert result.json()["error"] == "INVALID_REQUEST"
