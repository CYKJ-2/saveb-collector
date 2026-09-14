# 队列桥接：扫描持久化 outbox 投递任务，并将 Celery 同步入口转为异步业务调用。
import asyncio
from datetime import timedelta

from app.config.settings import get_settings
from app.domain.jobs import JobRequest
from app.domain.orders import SHANGHAI
from app.persistence.db import connect
from app.queue.celery_app import celery_app
from app.services.collection import run_chunk
from app.services.jobs import submit


# outbox 在任务终态前持续保留；重复投递由 worker 锁和分片状态消化，修复发送后崩溃的情况。
async def dispatch():
    async with connect() as conn:  # noqa: SIM117 - transaction lifetime is explicit
        # Sending while holding a row lock is bounded. Crash after send => duplicate, safe by design.
        async with conn.transaction():
            rows = await conn.fetch("""SELECT o.job_id,j.mode FROM collector.outbox o
                JOIN collector.jobs j ON j.id=o.job_id WHERE o.next_dispatch_at<=now()
                ORDER BY (j.mode IN ('refresh','today')) DESC,j.created_at
                LIMIT 50 FOR UPDATE OF o SKIP LOCKED""")
            sent = 0
            for row in rows:
                task = "collector.run_logistics" if row["mode"] == "logistics" else (
                    "collector.run"
                    if row["mode"] in ("refresh", "today")
                    else "collector.run_history"
                )
                try:
                    await asyncio.to_thread(
                        celery_app.send_task,
                        task,
                        args=[row["job_id"]],
                        queue="logistics" if row["mode"] == "logistics" else "collect" if row["mode"] in ("refresh", "today") else "history",
                        retry=False,
                    )
                except Exception as exc:  # noqa: BLE001 - leave durable outbox for retry
                    import logging

                    logging.getLogger(__name__).warning(
                        "outbox dispatch failed type=%s", type(exc).__name__
                    )
                    continue
                # Outbox remains a watchdog until task completion, also repairs worker crashes.
                await conn.execute(
                    "UPDATE collector.outbox SET next_dispatch_at=now()+interval '60 seconds' WHERE job_id=$1",
                    row["job_id"],
                )
                sent += 1
            return sent


@celery_app.task(name="collector.dispatch", ignore_result=True)
def dispatch_task():
    return asyncio.run(dispatch())


@celery_app.task(name="collector.schedule")
def schedule_task():
    return asyncio.run(check_schedule())


# 使用 collector.schedules 的实际间隔计算到期；scheduler_state 心跳表示调度检查，不表示采集完成。
async def check_schedule():
    account = get_settings().source_account
    async with connect() as conn:
        try:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO collector.schedules(account) VALUES($1) ON CONFLICT DO NOTHING",
                    account,
                )
                # Settings saves use this same row lock; due time and job commit together.
                schedule = await conn.fetchrow(
                    "SELECT * FROM collector.schedules WHERE account=$1 FOR UPDATE", account
                )
                now = await conn.fetchval("SELECT clock_timestamp()")
                job_id = None
                if schedule["next_run_at"] <= now:
                    # A slow scheduled job must not create an ever-growing automatic backlog.
                    active = await conn.fetchval(
                        "SELECT id FROM collector.jobs WHERE account=$1 AND actor='scheduler' "
                        "AND mode='refresh' AND status IN ('queued','running','retrying') "
                        "AND params->>'end'=$2 LIMIT 1",
                        account, now.astimezone(SHANGHAI).date().isoformat(),
                    )
                    if not active:
                        job = await submit(
                            conn, JobRequest(), "scheduler",
                            "due:" + schedule["next_run_at"].isoformat(),
                        )
                        job_id = job["jobId"]
                    await conn.execute(
                        "UPDATE collector.schedules SET next_run_at=$2 WHERE account=$1",
                        account, now + timedelta(minutes=schedule["interval_minutes"]),
                    )
                await conn.execute(
                    "INSERT INTO collector.scheduler_state(account,last_attempt_at,last_success_at) "
                    "VALUES($1,now(),now()) ON CONFLICT(account) DO UPDATE "
                    "SET last_attempt_at=now(),last_success_at=now(),error=NULL", account,
                )
                return job_id
        except Exception:
            await conn.execute(
                "INSERT INTO collector.scheduler_state(account,error) "
                "VALUES($1,'SCHEDULE_SUBMIT_FAILED') ON CONFLICT(account) DO UPDATE "
                "SET last_attempt_at=now(),error='SCHEDULE_SUBMIT_FAILED'", account,
            )
            raise


async def execute(job_id):
    async with connect() as conn:
        await run_chunk(conn, job_id)


@celery_app.task(name="collector.run", ignore_result=True)
def run_task(job_id):
    asyncio.run(execute(job_id))


@celery_app.task(name="collector.run_history", ignore_result=True)
def run_history_task(job_id):
    asyncio.run(execute(job_id))


@celery_app.task(name="collector.pending_schedule", ignore_result=True)
def pending_schedule_task():
    from app.services.pending import schedule_pending

    async def run():
        async with connect() as conn:
            return await schedule_pending(conn)
    return asyncio.run(run())


@celery_app.task(name="collector.reconcile", ignore_result=True)
def reconcile_task():
    from app.services.reconciliation import daily_reconcile

    result = asyncio.run(daily_reconcile())
    if result:
        import logging
        logging.getLogger(__name__).info("reconciliation id=%s status=%s", result["id"], result["status"])


@celery_app.task(name="collector.run_logistics", ignore_result=True)
def logistics_task(job_id):
    from app.services.logistics import run_chunk
    async def run():
        async with connect() as conn:
            await run_chunk(conn, job_id)
    asyncio.run(run())


@celery_app.task(name="collector.logistics_schedule", ignore_result=True)
def logistics_schedule_task():
    from app.services.logistics import schedule
    async def run():
        async with connect() as conn:
            await schedule(conn)
    asyncio.run(run())
