# 订单采集主流程：领取一个分片，抓取并归档，标准化后事务写库、更新统计与任务进度。
import asyncio
from datetime import UTC, date, datetime, timedelta

from app.collectors.dh_order import CollectionError, DHOrderClient
from app.collectors.product_images import enrich
from app.config.settings import get_settings
from app.domain.orders import digest, normalize, timestamp
from app.persistence.orders import upsert
from app.persistence.projections import rebuild


# 所有分片结束后汇总任务状态；成功时间取任务完成时间，不要求来源订单更新时间变化。
async def finalize(conn, job):
    counts = {
        r["status"]: r["n"]
        for r in await conn.fetch(
            "SELECT status,count(*) AS n FROM collector.chunks WHERE job_id=$1 GROUP BY status",
            job["id"],
        )
    }
    if counts.get("queued") or counts.get("running"):
        return
    failed = counts.get("failed", 0)
    state = (
        "cancelled"
        if job["cancel_requested"]
        else "partial_failed"
        if failed and counts.get("succeeded")
        else "failed"
        if failed
        else "succeeded"
    )
    await conn.execute(
        "UPDATE collector.jobs SET status=$2,updated_at=now() WHERE id=$1", job["id"], state
    )
    await conn.execute("DELETE FROM collector.outbox WHERE job_id=$1", job["id"])
    if job["mode"] == "pending" and state == "succeeded":
        from app.services.pending import finish_pending

        await finish_pending(conn, job)
    if job["mode"] in ("refresh", "today") and job["context"]["publish_api"]:
        stamp = datetime.now(UTC).isoformat()
        update = {
            "state": "success" if state == "succeeded" else "failed",
            "reason": "collector",
            "jobId": job["id"],
            "intervalMinutes": 30,
        }
        if state == "succeeded":
            update.update(lastSuccessAt=stamp, lastError="")
        else:
            update.update(lastFailureAt=stamp, lastError=state)
        await conn.execute(
            """INSERT INTO system_state(key,value) VALUES('refresh_status',$1)
            ON CONFLICT(key) DO UPDATE SET value=system_state.value || excluded.value,updated_at=now()""",
            update,
        )


# 每次投递处理一个分片；同账户来源锁覆盖网络请求和提交，防止并发采集互相覆盖。
async def run_chunk(conn, job_id, client_factory=DHOrderClient):
    job = await conn.fetchrow("SELECT * FROM collector.jobs WHERE id=$1", job_id)
    if not job or job["status"] in ("succeeded", "failed", "partial_failed", "cancelled"):
        return
    lock = "collector-source:" + job["account"]
    # Session advisory lock is held across HTTP AND commit on this same connection.
    # Connection loss invalidates the worker's ability to commit; no expiring lease race.
    if not await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1,0))", lock):
        return
    try:
        job = await conn.fetchrow("SELECT * FROM collector.jobs WHERE id=$1", job_id)
        if job["status"] in ("succeeded", "failed", "partial_failed", "cancelled"):
            return
        if job["context"].get("date_policy") != "source-create-time-v2":
            # Resume pre-upgrade jobs with the new date policy; retain their frozen rules/rates.
            job = dict(job)
            context = dict(job["context"])
            context["date_policy"] = "source-create-time-v2"
            context["version"] = digest({k: context[k] for k in ("rules", "rates", "date_policy", "items_policy")})
            await conn.execute("UPDATE collector.jobs SET context=$2 WHERE id=$1", job_id, context)
            job["context"] = context
        if job["cancel_requested"]:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE collector.chunks SET status='cancelled' WHERE job_id=$1 AND status IN ('queued','running')",
                    job_id,
                )
                await finalize(conn, job)
            return
        # Give pending realtime jobs precedence over the next historical chunk.
        if job["mode"] not in ("refresh", "today") and await conn.fetchval(
            "SELECT 1 FROM collector.jobs WHERE account=$1 AND mode IN ('refresh','today') AND status IN ('queued','running','retrying') LIMIT 1",
            job["account"],
        ):
            return
        chunk = await conn.fetchrow(
            "SELECT * FROM collector.chunks WHERE job_id=$1 AND status IN ('queued','running') ORDER BY id LIMIT 1",
            job_id,
        )
        if not chunk:
            async with conn.transaction():
                await finalize(conn, job)
            return
        await conn.execute(
            "UPDATE collector.jobs SET status='running',updated_at=now() WHERE id=$1", job_id
        )
        await conn.execute(
            "UPDATE collector.chunks SET status='running',attempts=attempts+1,error=NULL WHERE id=$1",
            chunk["id"],
        )
        try:
            if job["mode"] == "reprocess":
                rows = await conn.fetchval(
                    "SELECT raw FROM collector.chunks WHERE job_id=$1 AND scope->>'day'=$2 AND raw IS NOT NULL ORDER BY id LIMIT 1",
                    job["params"]["source_job_id"],
                    chunk["scope"].get("day"),
                )
                if rows is None:
                    raise CollectionError("RAW_EVIDENCE_MISSING")
            else:
                async with client_factory() as client:
                    if job["mode"] == "pending":
                        async with asyncio.timeout(get_settings().pending_timeout_seconds):
                            rows = await client.collect(chunk["scope"])
                    else:
                        rows = await client.collect(chunk["scope"])
                rows = await enrich(rows, job["context"])
            # Archive before normalization so invalid payloads can be investigated/replayed.
            # 原始响应先独立归档；后续标准化或写库失败时仍可定位问题。
            await conn.execute(
                "UPDATE collector.chunks SET raw=$2,fetched_at=now() WHERE id=$1", chunk["id"], rows
            )
            normalized = [normalize(row, job["context"]) for row in rows]
            # Keep padding-window rows too: an old order may now belong to a new business day.
            # Filtering by its NEW accounting date here would hide precisely that correction.
            selected = list(zip(normalized, rows))
            if job["mode"] == "pending":
                selected = [(n, raw) for n, raw in selected if n["columns"]["order_status"] == "pending"]
            if job["mode"] == "today":
                # 手动按钮只发布本任务北京时间当天的订单；历史纠正由定时与回填任务处理。
                selected = [(n, raw) for n, raw in selected if n["day"] == chunk["scope"]["day"]]
            counts = {
                "fetched": len(rows),
                "selected": len(selected),
                "inserted": 0,
                "updated": 0,
                "unchanged": 0,
                "stale": 0,
            }
            # 业务订单、统计、覆盖标记和成功状态一起提交，异常时整片回滚。
            async with conn.transaction():
                cancelled = await conn.fetchval(
                    "SELECT cancel_requested FROM collector.jobs WHERE id=$1 FOR UPDATE", job_id
                )
                if cancelled:
                    await conn.execute(
                        "UPDATE collector.chunks SET status='cancelled' WHERE id=$1", chunk["id"]
                    )
                    return
                days = set()
                if job["params"]["dry_run"]:
                    for n, raw in selected:
                        prior = await conn.fetchrow(
                            "SELECT * FROM collector.source_orders WHERE account=$1 AND order_id=$2",
                            job["account"],
                            n["id"],
                        )
                        if not prior:
                            counts["inserted"] += 1
                        elif (
                            n["source_updated_at"]
                            and prior["source_updated_at"]
                            and timestamp(n["source_updated_at"]) < prior["source_updated_at"]
                        ):
                            counts["stale"] += 1
                        elif prior["content_hash"] == digest(
                            {"raw": raw, "context": n["context_version"]}
                        ):
                            counts["unchanged"] += 1
                        else:
                            counts["updated"] += 1
                if not job["params"]["dry_run"]:
                    # Acquire all affected date locks before row updates for consistent stats.
                    if job["context"]["publish_api"]:
                        old_days = await conn.fetch(
                            "SELECT DISTINCT (order_time AT TIME ZONE 'Asia/Shanghai')::date AS day FROM orders WHERE order_id=ANY($1::text[])",
                            [n["id"] for n, _ in selected],
                        )
                        days = {r["day"] for r in old_days if r["day"]} | {
                            date.fromisoformat(n["day"]) for n, _ in selected
                        }
                        if "day" in chunk["scope"]:
                            days.add(date.fromisoformat(chunk["scope"]["day"]))
                        for day in sorted(days):
                            await conn.execute(
                                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                                f"collector-stats:{day}",
                            )
                    for n, raw in selected:
                        state, changed_days = await upsert(
                            conn, job["account"], n, raw, job_id, job["context"]["publish_api"]
                        )
                        counts[state] += 1
                        days |= changed_days
                    if job["context"]["publish_api"]:
                        await rebuild(conn, days, job_id)
                        audit_days = days or (
                            {date.fromisoformat(chunk["scope"]["pending_start"]),
                             date.fromisoformat(chunk["scope"]["pending_end"])}
                            if job["mode"] == "pending" else set()
                        )
                        if audit_days:
                            from app.services.reconciliation import reconcile

                            audit = await reconcile(
                                conn, min(audit_days), max(audit_days), job_id=job_id, chunk_id=chunk["id"],
                                order_ids=list({n["id"] for n, _ in selected}),
                                checked_days=sorted(audit_days),
                            )
                            counts["reconciliation_id"] = audit["id"]
                            counts["reconciliation_status"] = audit["status"]
                    if job["mode"] == "pending":
                        from app.services.pending import commit_pending

                        await commit_pending(conn, job, chunk["scope"], len(rows))
                    # 只有完整日期分片成功才写覆盖记录；单号复查不会宣称整天已采集。
                    if "day" in chunk["scope"]:
                        day = date.fromisoformat(chunk["scope"]["day"])
                        await conn.execute(
                            "INSERT INTO collector.coverage(account,day,job_id,published) VALUES($1,$2,$3,$4) ON CONFLICT(account,day) DO UPDATE SET job_id=excluded.job_id,published=excluded.published,completed_at=now()",
                            job["account"],
                            day,
                            job_id,
                            job["context"]["publish_api"],
                        )
                        if chunk["scope"].get("rolling"):
                            await conn.execute(
                                "INSERT INTO collector.checkpoints(account,history_cursor) VALUES($1,$2) ON CONFLICT(account) DO UPDATE SET history_cursor=excluded.history_cursor",
                                job["account"],
                                day + timedelta(days=1),
                            )
                await conn.execute(
                    "UPDATE collector.chunks SET status='succeeded',counts=$2,normalized=$3,committed_at=now() WHERE id=$1",
                    chunk["id"],
                    counts,
                    [n for n, _ in selected],
                )
                await finalize(conn, job)
                await conn.execute(
                    "UPDATE collector.outbox SET next_dispatch_at=now() WHERE job_id=$1", job_id
                )
        except Exception as exc:  # noqa: BLE001 - persist failure while redacting source/SQL data
            # Avoid logging SQL parameters or upstream customer information in public status.
            code = str(exc) if isinstance(exc, CollectionError) else "COLLECTION_INTERNAL_ERROR"
            if job["mode"] == "pending" and isinstance(exc, TimeoutError):
                code = "PENDING_TIME_BUDGET_EXCEEDED"
            # 历史 Pending 超分页或超时预算时二分区间，保留父片为 split，子片继续执行。
            if job["mode"] == "pending" and code in ("PAGE_BUDGET_EXCEEDED", "PENDING_TIME_BUDGET_EXCEEDED"):
                start = date.fromisoformat(chunk["scope"]["pending_start"])
                end = date.fromisoformat(chunk["scope"]["pending_end"])
                if start < end:
                    middle = start + (end - start) // 2
                    scopes = [
                        {"pending_start": start.isoformat(), "pending_end": middle.isoformat()},
                        {"pending_start": (middle + timedelta(days=1)).isoformat(), "pending_end": end.isoformat()},
                    ]
                    async with conn.transaction():
                        await conn.execute("UPDATE collector.chunks SET status='split',error=$2 WHERE id=$1", chunk["id"], code)
                        await conn.execute(
                            "INSERT INTO collector.chunks(job_id,scope) SELECT $1,value FROM jsonb_array_elements($2::jsonb) ON CONFLICT DO NOTHING",
                            job_id, scopes,
                        )
                        await conn.execute("UPDATE collector.outbox SET next_dispatch_at=now() WHERE job_id=$1", job_id)
                    return
            permanent = code.startswith(
                ("AUTH_", "SOURCE_VERSION", "ORDER_", "CURRENCY_", "PRODUCT_", "RAW_", "API_")
            )
            retry = not permanent and chunk["attempts"] + 1 < get_settings().max_attempts
            async with conn.transaction():
                await conn.execute(
                    "UPDATE collector.chunks SET status=$2,error=$3 WHERE id=$1",
                    chunk["id"],
                    "queued" if retry else "failed",
                    code,
                )
                await conn.execute(
                    "UPDATE collector.jobs SET status=$2,error=$3,updated_at=now() WHERE id=$1",
                    job_id,
                    "retrying" if retry else "running",
                    code,
                )
                await conn.execute(
                    "UPDATE collector.outbox SET next_dispatch_at=now()+interval '30 seconds' WHERE job_id=$1",
                    job_id,
                )
                await finalize(conn, job)
            if not isinstance(exc, CollectionError):
                # Worker logs get exception TYPE only. Detailed failed evidence remains in DB.
                import logging

                logging.getLogger(__name__).error(
                    "collection failure type=%s job=%s", type(exc).__name__, job_id
                )
    finally:
        await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", lock)
