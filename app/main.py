# HTTP 服务入口：校验内部服务令牌，接收任务与状态请求；耗时采集由队列执行。
"""Authenticated job API; business invoice/procurement writes stay in ERP."""

import hmac
from typing import Annotated

from litestar import Litestar, Request, get, post
from litestar.exceptions import HTTPException
from litestar.params import PathParameter
from litestar.response import Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
from pydantic import ValidationError

from app.collectors.dh_order import CollectionError
from app.config.settings import get_settings
from app.domain.jobs import JobRequest
from app.persistence.db import connect, encode
from app.services.jobs import cancel, resume, status, submit


# 校验 API 与 Collector 之间的内部令牌；用户角色和按钮权限由 saveb-api 校验。
def authorize(request):
    expected = get_settings().api_token.get_secret_value()
    if not expected:
        raise HTTPException(status_code=503, detail="COLLECTOR_TOKEN_NOT_CONFIGURED")
    actual = request.headers.get("authorization", "")
    if not hmac.compare_digest(actual.encode(), ("Bearer " + expected).encode()):
        raise HTTPException(status_code=401, detail="UNAUTHORIZED")


def response(value, code=200):
    return Response(content=encode(value), media_type="application/json", status_code=code)


@get("/health", sync_to_thread=False)
def health() -> dict:
    return {"status": "ok", "service": "saveb-collector"}


@get("/ready")
async def ready() -> Response:
    try:
        async with connect() as conn:
            version = await conn.fetchval("SELECT max(version) FROM collector.schema_versions")
            if version != 5:
                raise ValueError()
    except Exception:  # noqa: BLE001 - readiness must return a bounded error without DB details
        return response({"status": "dependency_unavailable"}, 503)
    return response({"status": "ready"})


@get("/metrics")
async def metrics(request: Request) -> Response:
    authorize(request)
    registry = CollectorRegistry()
    jobs = Gauge("saveb_collector_jobs", "Persisted jobs by state", ["state"], registry=registry)
    age = Gauge(
        "saveb_collector_oldest_active_seconds", "Oldest nonterminal job age", registry=registry
    )
    async with connect() as conn:
        for row in await conn.fetch(
            "SELECT status,count(*) AS n FROM collector.jobs GROUP BY status"
        ):
            jobs.labels(state=row["status"]).set(row["n"])
        seconds = await conn.fetchval(
            "SELECT extract(epoch from now()-min(created_at)) FROM collector.jobs WHERE status IN ('queued','running','retrying')"
        )
        age.set(float(seconds or 0))
    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


# 接口只受理并持久化任务；客户端应继续查询任务状态，不能把受理当作采集完成。
@post("/api/collect/jobs")
async def create_job(request: Request) -> Response:
    authorize(request)
    try:
        data = JobRequest.model_validate(await request.json())
        async with connect() as conn:
            value = await submit(
                conn,
                data,
                request.headers.get("x-collector-actor", "api"),
                request.headers.get("idempotency-key", ""),
            )
        return response(value, 202)
    except (ValueError, ValidationError) as exc:
        return response(
            {
                "error": "INVALID_REQUEST",
                "detail": "IDEMPOTENCY_KEY_CONFLICT"
                if "IDEMPOTENCY_KEY_CONFLICT" in str(exc)
                else "Check mode, dates and Idempotency-Key",
            },
            400,
        )
    except CollectionError as exc:
        return response({"error": str(exc)}, 422)


@get("/api/collect/jobs/{job_id:str}")
async def get_job(request: Request, job_id: Annotated[str, PathParameter()]) -> Response:
    authorize(request)
    async with connect() as conn:
        value = await status(conn, job_id)
    return response(value if value else {"error": "JOB_NOT_FOUND"}, 200 if value else 404)


@post("/api/collect/jobs/{job_id:str}/resume")
async def retry_job(request: Request, job_id: Annotated[str, PathParameter()]) -> Response:
    authorize(request)
    try:
        async with connect() as conn:
            return response(await resume(conn, job_id), 202)
    except LookupError:
        return response({"error": "JOB_NOT_FOUND"}, 404)
    except ValueError:
        return response({"error": "JOB_NOT_FAILED"}, 409)


@post("/api/collect/jobs/{job_id:str}/cancel")
async def cancel_job(request: Request, job_id: Annotated[str, PathParameter()]) -> Response:
    authorize(request)
    try:
        async with connect() as conn:
            return response(await cancel(conn, job_id), 202)
    except LookupError:
        return response({"error": "JOB_NOT_FOUND"}, 404)


@get("/api/logistics/status")
async def logistics_status(request: Request) -> Response:
    authorize(request)
    from app.services.logistics import status
    async with connect() as conn:
        return response(await status(conn, request.query_params.get("jobId")))


@post("/api/logistics/refresh")
async def logistics_refresh(request: Request) -> Response:
    authorize(request)
    from app.collectors.logistics import LogisticsError
    from app.services.logistics import LogisticsRequest, submit
    try:
        data = LogisticsRequest.model_validate(await request.json())
        async with connect() as conn:
            result = await submit(conn, data, request.headers.get("x-collector-actor", "api"), request.headers.get("idempotency-key", ""))
        return response(result, 202)
    except LogisticsError as error:
        return response({"error": str(error)}, 422)
    except ValueError:
        return response({"error": "INVALID_LOGISTICS_REQUEST"}, 400)


app = Litestar(
    route_handlers=[health, ready, metrics, create_job, get_job, retry_job, cancel_job, logistics_status, logistics_refresh],
    debug=False,
    request_max_body_size=16384,
)
