# 任务规划：幂等提交、按日期或订单号生成分片，并在同一事务写入待投递记录。
from datetime import date, datetime, timedelta
from uuid import uuid4

from app.config.settings import get_settings
from app.domain.jobs import JobRequest
from app.domain.orders import SHANGHAI, digest
from app.services.context import load_context

TERMINAL = {"succeeded", "partial_failed", "failed", "cancelled"}


async def status(conn, job_id):
    job = await conn.fetchrow(
        "SELECT id, mode, actor, status, cancel_requested, error, created_at, updated_at, params, CASE WHEN (params->>'dry_run')::boolean THEN 'preview' WHEN (context->>'publish_api')::boolean THEN 'saveb-api' ELSE 'shadow' END AS publication FROM collector.jobs WHERE id=$1",
        job_id,
    )
    if not job:
        return None
    chunks = await conn.fetch(
        "SELECT id,scope,status,attempts,error,counts,committed_at FROM collector.chunks WHERE job_id=$1 ORDER BY id",
        job_id,
    )
    result = dict(job)
    result["jobId"] = job_id
    result["chunks"] = [dict(c) for c in chunks]
    result["completed"] = sum(c["status"] == "succeeded" for c in chunks)
    result["total"] = sum(c["status"] != "split" for c in chunks)
    result["split"] = sum(c["status"] == "split" for c in chunks)
    result["statusUrl"] = f"/api/collect/jobs/{job_id}"
    return result


# 同一操作者的幂等键只能对应一份参数；任务与 outbox 同事务提交，避免已受理却未入队。
async def submit(conn, request: JobRequest, actor: str, key: str):
    if not key or len(key) > 200 or not actor or len(actor) > 200:
        raise ValueError("actor and idempotency key must be 1..200 characters")
    settings = get_settings()
    account = settings.source_account
    today = datetime.now(SHANGHAI).date()
    params = request.model_dump(mode="json")
    if request.mode == "today":
        params.update(start=today.isoformat(), end=today.isoformat())
    fingerprint = digest(params)
    async with conn.transaction():
        # Serialize job planning, independent of the long-running source fetch lock.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "collector-plan:" + account
        )
        existing = await conn.fetchrow(
            "SELECT id,request_hash FROM collector.jobs WHERE account=$1 AND actor=$2 AND idempotency_key=$3",
            account,
            actor,
            key,
        )
        if existing:
            if existing["request_hash"] != fingerprint:
                raise ValueError("IDEMPOTENCY_KEY_CONFLICT")
            return await status(conn, existing["id"])
        # Coalesce matching work for the same actor. Other users get their own audited job.
        active = await conn.fetchval(
            "SELECT id FROM collector.jobs WHERE account=$1 AND actor=$2 AND request_hash=$3 AND status IN ('queued','running','retrying') ORDER BY created_at LIMIT 1",
            account,
            actor,
            fingerprint,
        )
        if active:
            return await status(conn, active)
        start = request.start or today - timedelta(days=request.lookback_days - 1)
        end = request.end or today
        if request.mode == "today":
            start = end = today
        if end > today:
            raise ValueError("end cannot be in the future")
        scopes = [
            {"day": (start + timedelta(days=i)).isoformat()} for i in range((end - start).days + 1)
        ]
        if request.mode == "pending":
            width = settings.pending_window_days
            scopes = [
                {"pending_start": (start + timedelta(days=i)).isoformat(),
                 "pending_end": min(end, start + timedelta(days=i + width - 1)).isoformat()}
                for i in range(0, (end - start).days + 1, width)
            ]
        # 补缺依据成功覆盖记录筛选日期，不以业务表当天是否已有一条订单作为依据。
        if request.mode == "missing":
            covered = {
                r["day"].isoformat()
                for r in await conn.fetch(
                    "SELECT day FROM collector.coverage WHERE account=$1 AND day BETWEEN $2 AND $3 AND (NOT $4::boolean OR published)",
                    account,
                    start,
                    end,
                    settings.publish_api and not request.dry_run,
                )
            }
            scopes = [s for s in scopes if s["day"] not in covered]
        # 防漏由近期日期重采、已知未完成单号复查、滚动历史日三部分组成。
        if request.mode == "refresh":
            if request.include_open_orders:
                opened = await conn.fetch(
                    "SELECT order_id FROM collector.source_orders WHERE account=$1 AND normalized->'columns'->>'order_status' = ANY($2::text[])",
                    account,
                    ["pending", "reversed", "failed", "expired"],
                )
                scopes += [{"order_id": r["order_id"]} for r in opened]
                # Bootstrap from existing ERP open orders before source_orders has been seeded.
                if await conn.fetchval("SELECT to_regclass('public.orders')"):
                    inherited = await conn.fetch(
                        "SELECT order_id FROM orders WHERE deleted_at IS NULL AND lower(order_status)=ANY($1::text[])",
                        ["pending", "reversed", "failed", "expired"],
                    )
                    scopes += [{"order_id": r["order_id"]} for r in inherited]
            history_start = date.fromisoformat(settings.history_start)
            cursor = (
                await conn.fetchval(
                    "SELECT history_cursor FROM collector.checkpoints WHERE account=$1", account
                )
                or history_start
            )
            if cursor >= start:
                cursor = history_start
            if cursor < start:
                scopes.append({"day": cursor.isoformat(), "rolling": True})
        context = await load_context(conn)
        context["publish_api"] = settings.publish_api and not request.dry_run
        # Freeze publication mode/context so a configuration change cannot alter an old job.
        if request.mode == "reprocess":
            source = await conn.fetchrow(
                "SELECT account FROM collector.jobs WHERE id=$1", request.source_job_id
            )
            if not source or source["account"] != account:
                raise ValueError("SOURCE_JOB_NOT_FOUND")
            archived = {
                row["day"] for row in await conn.fetch(
                    "SELECT scope->>'day' AS day FROM collector.chunks "
                    "WHERE job_id=$1 AND raw IS NOT NULL", request.source_job_id,
                )
            }
            if any(scope["day"] not in archived for scope in scopes):
                raise ValueError("RAW_DATE_RANGE_NOT_ARCHIVED")
        job_id = str(uuid4())
        await conn.execute(
            "INSERT INTO collector.jobs(id,account,mode,actor,idempotency_key,request_hash,params,context) VALUES($1,$2,$3,$4,$5,$6,$7,$8)",
            job_id,
            account,
            request.mode,
            actor,
            key,
            fingerprint,
            params,
            context,
        )
        if scopes:
            await conn.execute(
                "INSERT INTO collector.chunks(job_id,scope) SELECT $1,value FROM jsonb_array_elements($2::jsonb) WITH ORDINALITY AS s(value,n) ORDER BY n ON CONFLICT DO NOTHING",
                job_id,
                scopes,
            )
        if scopes:
            await conn.execute("INSERT INTO collector.outbox(job_id) VALUES($1)", job_id)
        else:
            await conn.execute("UPDATE collector.jobs SET status='succeeded' WHERE id=$1", job_id)
        return await status(conn, job_id)


# 仅把失败分片重新排队，已成功分片不重采；继续使用原任务的规则快照。
async def resume(conn, job_id):
    async with conn.transaction():
        row = await conn.fetchrow("SELECT * FROM collector.jobs WHERE id=$1 FOR UPDATE", job_id)
        if not row:
            raise LookupError("JOB_NOT_FOUND")
        if row["status"] not in ("failed", "partial_failed"):
            raise ValueError("ONLY_FAILED_JOBS_CAN_RESUME")
        await conn.execute(
            "UPDATE collector.chunks SET status='queued', attempts=0,error=NULL WHERE job_id=$1 AND status='failed'",
            job_id,
        )
        await conn.execute(
            "UPDATE collector.jobs SET status='queued',error=NULL,updated_at=now() WHERE id=$1",
            job_id,
        )
        await conn.execute(
            "INSERT INTO collector.outbox(job_id) VALUES($1) ON CONFLICT(job_id) DO UPDATE SET next_dispatch_at=now()",
            job_id,
        )
    return await status(conn, job_id)


# 这里只设置取消标记；worker 在写库前再次检查，已提交的分片不会回滚。
async def cancel(conn, job_id):
    result = await conn.execute(
        "UPDATE collector.jobs SET cancel_requested=true WHERE id=$1 AND status NOT IN ('succeeded','failed','partial_failed','cancelled')",
        job_id,
    )
    if result == "UPDATE 0" and not await status(conn, job_id):
        raise LookupError("JOB_NOT_FOUND")
    return await status(conn, job_id)
