# 物流任务服务：规划采购单号查询、独立调度、保护并发人工编辑并写入物流事件。
"""Durable logistics jobs, isolated from DH order collection and its source lock."""
import asyncio
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.collectors.logistics import LogisticsError, provider_name, query_tracking
from app.config.settings import get_settings
from app.domain.orders import digest, timestamp


class LogisticsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    provider: Literal["auto", "aftership", "kuaidi100"] = "auto"
    task_id: int | None = Field(None, alias="taskId", gt=0)


# 冻结单号、承运商和电话作为查询身份；提交结果前必须与当前采购记录一致。
def signature(record):
    raw = record["raw"] or {}
    return {"number": str(record["tracking_no"] or "").strip(), "carrier": str(raw.get("trackingCarrier") or "").strip(),
            "phone": str(raw.get("trackingPhone") or "").strip()}


async def job_status(conn, job_id):
    row = await conn.fetchrow("SELECT id,status,error,created_at,updated_at FROM collector.jobs WHERE id=$1 AND mode='logistics' AND account=$2",
                              job_id, get_settings().source_account)
    if not row:
        return None
    counts = await conn.fetchrow("SELECT count(*) AS total,count(*) FILTER(WHERE status='succeeded') AS completed,"
                                 "count(*) FILTER(WHERE status='failed') AS failed,"
                                 "coalesce(sum((counts->>'updated')::int),0) AS updated,"
                                 "coalesce(sum((counts->>'skipped')::int),0) AS skipped "
                                 "FROM collector.chunks WHERE job_id=$1", job_id)
    return {"jobId": row["id"], **dict(row), **dict(counts)}


async def status(conn, job_id=None):
    settings = get_settings()
    try:
        provider = provider_name()
    except LogisticsError:
        provider = None
    if job_id is None:
        job_id = await conn.fetchval("SELECT id FROM collector.jobs WHERE mode='logistics' AND account=$1 ORDER BY created_at DESC LIMIT 1", settings.source_account)
    return {"configured": provider is not None, "provider": provider,
            "autoEnabled": settings.logistics_enabled and settings.publish_api and provider is not None,
            "intervalMinutes": settings.logistics_interval_minutes, "batchSize": settings.logistics_batch_size,
            "providers": {"aftership": bool(settings.aftership_api_key.get_secret_value()), "kuaidi100": bool(settings.kuaidi100_api_key.get_secret_value())},
            "job": await job_status(conn, job_id) if job_id else None}


# 自动查询按检查时间筛选一批任务；手动查询允许复查已签收记录，数量仍受批次上限约束。
async def submit(conn, request, actor, key, automatic=False):
    settings = get_settings()
    if not settings.publish_api:
        raise LogisticsError("API_PUBLICATION_DISABLED")
    provider = provider_name(request.provider)
    if not key or len(key) > 200 or not actor or len(actor) > 200:
        raise ValueError("Invalid actor or idempotency key")
    params = {**request.model_dump(), "dry_run": False, "automatic": automatic}
    fingerprint = digest(params)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "logistics-plan:" + settings.source_account)
        existing = await conn.fetchrow("SELECT id,request_hash FROM collector.jobs WHERE account=$1 AND actor=$2 AND idempotency_key=$3", settings.source_account, actor, key)
        if existing:
            if existing["request_hash"] != fingerprint:
                raise ValueError("IDEMPOTENCY_KEY_CONFLICT")
            return await job_status(conn, existing["id"])
        active = await conn.fetchval("SELECT id FROM collector.jobs WHERE account=$1 AND mode='logistics' AND actor=$2 AND request_hash=$3 AND status IN ('queued','running','retrying') LIMIT 1",
                                     settings.source_account, actor, fingerprint)
        if active:
            return await job_status(conn, active)
        # Oldest checked first prevents the same first batch starving later tracking numbers.
        records = await conn.fetch("SELECT id,tracking_no,raw FROM public.procurement_tasks WHERE deleted_at IS NULL "
            "AND nullif(trim(tracking_no),'') IS NOT NULL AND ($1::bigint IS NULL OR id=$1) "
            "ORDER BY coalesce(raw->>'trackingLastCheckedAt',''),id", request.task_id)
        if request.task_id and not records:
            raise LogisticsError("TRACKING_TASK_NOT_FOUND")
        now = await conn.fetchval("SELECT clock_timestamp()")
        scopes = []
        for record in records:
            raw = record["raw"] or {}
            scope = signature(record)
            if automatic and raw.get("trackingQueriedNumber") == scope["number"]:
                if raw.get("deliveryStatus") in ("delivered", "expired"):
                    continue
                try:
                    last = timestamp(raw.get("trackingLastCheckedAt"))
                except Exception:  # noqa: BLE001 - malformed legacy metadata must be refreshed
                    last = None
                if last and now - last < timedelta(minutes=settings.logistics_interval_minutes):
                    continue
            scope["task_id"] = record["id"]
            if raw.get("trackingProvider") == provider and raw.get("trackingQueriedNumber") == scope["number"]:
                scope["provider_id"] = raw.get("trackingProviderId", "")
            scopes.append(scope)
            if len(scopes) >= settings.logistics_batch_size:
                break
        job_id = str(uuid4())
        await conn.execute("INSERT INTO collector.jobs(id,account,mode,actor,idempotency_key,request_hash,params,context,status) VALUES($1,$2,'logistics',$3,$4,$5,$6,$7,$8)",
                           job_id, settings.source_account, actor, key, fingerprint, params,
                           {"provider": provider, "publish_api": False}, "queued" if scopes else "succeeded")
        for scope in scopes:
            await conn.execute("INSERT INTO collector.chunks(job_id,scope) VALUES($1,$2)", job_id, scope)
        if scopes:
            await conn.execute("INSERT INTO collector.outbox(job_id) VALUES($1)", job_id)
        return await job_status(conn, job_id)


# 物流间隔独立于 DH 采集间隔；无供应商密钥或已有物流任务运行时不再提交。
async def schedule(conn):
    settings = get_settings()
    if not settings.logistics_enabled or not settings.publish_api:
        return
    try:
        provider_name()
    except LogisticsError:
        return
    async with conn.transaction():
        await conn.execute("INSERT INTO collector.logistics_schedule(account) VALUES($1) ON CONFLICT DO NOTHING", settings.source_account)
        state = await conn.fetchrow("SELECT * FROM collector.logistics_schedule WHERE account=$1 FOR UPDATE", settings.source_account)
        now = await conn.fetchval("SELECT clock_timestamp()")
        if state["next_run_at"] > now or await conn.fetchval("SELECT 1 FROM collector.jobs WHERE mode='logistics' AND account=$1 AND status IN ('queued','running','retrying') LIMIT 1", settings.source_account):
            return
        job = await submit(conn, LogisticsRequest(), "logistics-scheduler", "logistics:" + state["next_run_at"].isoformat(), True)
        await conn.execute("UPDATE collector.logistics_schedule SET next_run_at=$2,last_job_id=$3,updated_at=now() WHERE account=$1", settings.source_account,
                           now + timedelta(minutes=settings.logistics_interval_minutes), job["jobId"])


# 独立物流锁避免与 DH 来源锁互相阻塞；逐条请求后只更新物流字段和事件。
async def run_chunk(conn, job_id, query=query_tracking):
    job = await conn.fetchrow("SELECT * FROM collector.jobs WHERE id=$1 AND mode='logistics'", job_id)
    if not job or job["status"] in ("succeeded", "failed", "partial_failed", "cancelled"):
        return
    lock = "collector-logistics:" + job["account"]
    if not await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1,0))", lock):
        return
    chunk = None
    try:
        job = await conn.fetchrow("SELECT * FROM collector.jobs WHERE id=$1", job_id)
        if job["status"] in ("succeeded", "failed", "partial_failed", "cancelled"):
            return
        chunk = await conn.fetchrow("SELECT * FROM collector.chunks WHERE job_id=$1 AND status IN ('queued','running') ORDER BY id LIMIT 1", job_id)
        if chunk and not job["cancel_requested"]:
            await conn.execute("UPDATE collector.jobs SET status='running',updated_at=now() WHERE id=$1", job_id)
            await conn.execute("UPDATE collector.chunks SET status='running',attempts=attempts+1 WHERE id=$1", chunk["id"])
            result, error = None, None
            try:
                async with asyncio.timeout(80):
                    result = await query(job["context"]["provider"], chunk["scope"])
            except Exception as exc:  # noqa: BLE001 - do not log provider URL / credentials
                error = str(exc) if isinstance(exc, LogisticsError) else "PROVIDER_QUERY_FAILED"
            async with conn.transaction():
                cancelled = await conn.fetchval("SELECT cancel_requested FROM collector.jobs WHERE id=$1 FOR UPDATE", job_id)
                record = await conn.fetchrow("SELECT id,tracking_no,raw,deleted_at FROM public.procurement_tasks WHERE id=$1 FOR UPDATE", chunk["scope"]["task_id"])
                # HTTP 返回期间采购信息可能已变更；持有行锁后重新核对查询身份再写结果。
                skipped = cancelled or not record or record["deleted_at"] or signature(record) != {key: chunk["scope"][key] for key in ("number", "carrier", "phone")}
                updated = 0
                if not skipped:
                    now = await conn.fetchval("SELECT clock_timestamp()")
                    patch = {"trackingLastCheckedAt": now.isoformat(), "trackingError": error or "",
                             "trackingQueriedNumber": chunk["scope"]["number"], "trackingProvider": job["context"]["provider"]}
                    if error and (record["raw"] or {}).get("trackingQueriedNumber") != chunk["scope"]["number"]:
                        patch.update(deliveryStatus="unknown", trackingProviderId="", trackingCheckpoint="", deliveredAt=None)
                    if result:
                        patch.update(deliveryStatus=result["status"], trackingCheckpoint=result["checkpoint"],
                                     trackingUpdatedAt=result["occurred_at"], trackingProviderId=result["provider_id"],
                                     deliveredAt=result["occurred_at"] if result["status"] == "delivered" else None)
                        # 去重键不含本次检查时间，相同轨迹重复查询不会产生重复物流事件。
                        event_id = digest({"task": record["id"], "result": {k:v for k,v in result.items() if k != "checked_at"}})
                        await conn.execute("INSERT INTO public.shipment_tracking_events(procurement_task_id,provider,tracking_number,status,event_id,raw,occurred_at) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(provider,tracking_number,event_id) DO NOTHING",
                                           record["id"], job["context"]["provider"], chunk["scope"]["number"], result["status"], event_id, result, timestamp(result["occurred_at"]))
                        updated = 1
                    await conn.execute("UPDATE public.procurement_tasks SET raw=coalesce(raw,'{}'::jsonb)||$2::jsonb,version=version+1,updated_at=now() WHERE id=$1", record["id"], patch)
                retry = bool(error and not skipped and error not in ("PROVIDER_AUTH_FAILED", "PROVIDER_QUOTA_EXHAUSTED") and chunk["attempts"] + 1 < 3)
                await conn.execute("UPDATE collector.chunks SET status=$2,error=$3,counts=$4,raw=$5,committed_at=now() WHERE id=$1",
                                   chunk["id"], "queued" if retry else "failed" if error and not skipped else "succeeded", error if not skipped else None,
                                   {"updated": updated, "skipped": int(bool(skipped))}, result)
                await conn.execute("UPDATE collector.jobs SET updated_at=now(),error=$2 WHERE id=$1", job_id, error if not skipped else None)
                await conn.execute("UPDATE collector.outbox SET next_dispatch_at=now()+interval '30 seconds' WHERE job_id=$1", job_id)
        async with conn.transaction():
            cancelled = await conn.fetchval("SELECT cancel_requested FROM collector.jobs WHERE id=$1 FOR UPDATE", job_id)
            if cancelled:
                await conn.execute("UPDATE collector.chunks SET status='cancelled' WHERE job_id=$1 AND status IN ('queued','running')", job_id)
            left = await conn.fetchval("SELECT count(*) FROM collector.chunks WHERE job_id=$1 AND status IN ('queued','running')", job_id)
            if not left:
                failed = await conn.fetchval("SELECT count(*) FROM collector.chunks WHERE job_id=$1 AND status='failed'", job_id)
                succeeded = await conn.fetchval("SELECT count(*) FROM collector.chunks WHERE job_id=$1 AND status='succeeded'", job_id)
                state = "cancelled" if cancelled else "partial_failed" if failed and succeeded else "failed" if failed else "succeeded"
                await conn.execute("UPDATE collector.jobs SET status=$2,updated_at=now() WHERE id=$1", job_id, state)
                await conn.execute("DELETE FROM collector.outbox WHERE job_id=$1", job_id)
    except Exception:
        if chunk:
            async with conn.transaction():
                retry = chunk["attempts"] + 1 < 3
                await conn.execute("UPDATE collector.chunks SET status=$2,error='LOGISTICS_INTERNAL_ERROR' WHERE id=$1", chunk["id"], "queued" if retry else "failed")
                await conn.execute("UPDATE collector.jobs SET error='LOGISTICS_INTERNAL_ERROR',updated_at=now() WHERE id=$1", job_id)
                await conn.execute("UPDATE collector.outbox SET next_dispatch_at=now()+interval '30 seconds' WHERE job_id=$1", job_id)
        else:
            raise
    finally:
        await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", lock)
