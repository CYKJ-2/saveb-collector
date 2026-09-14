# 独立发现历史 Pending：有界扫描尚未记录的未完成订单，供后续按订单号持续复查。
"""Independent, bounded discovery of previously unknown historical Pending orders."""
from datetime import date, datetime, timedelta

from app.config.settings import get_settings
from app.domain.jobs import JobRequest
from app.domain.orders import SHANGHAI
from app.services.jobs import submit


def pending_bounds(today=None):
    """按北京时间滑动窗口；历史起始日仅作为更晚的下限。"""
    settings = get_settings()
    today = today or datetime.now(SHANGHAI).date()
    lower = max(date.fromisoformat(settings.history_start),
                today - timedelta(days=settings.pending_lookback_days - 1))
    return lower, today


def automatic_pending_outdated(job):
    """升级遗留、跨日过期的自动任务停止续抓，人工指定范围不受影响。"""
    if job["mode"] != "pending" or job["actor"] != "pending-scheduler":
        return False
    lower, today = pending_bounds()
    return (date.fromisoformat(job["params"]["start"]) < lower
            or date.fromisoformat(job["params"]["end"]) > today)


# 到期只提交一个历史窗口；已有 Pending 任务运行时复用，避免不断堆积。
async def schedule_pending(conn):
    settings = get_settings()
    if not settings.pending_enabled:
        return None
    account = settings.source_account
    lower, today = pending_bounds()
    if lower > today:
        return None
    async with conn.transaction():
        # 先锁任务再锁游标，与 worker 提交的锁顺序一致。保留已成功分片和业务数据，
        # 未完成分片由 worker 在来源锁内取消；在途响应也会被提交前的取消检查拦住。
        await conn.execute(
            "UPDATE collector.jobs SET cancel_requested=true,error='PENDING_WINDOW_EXPIRED',updated_at=now() "
            "WHERE account=$1 AND mode='pending' AND actor='pending-scheduler' "
            "AND status IN ('queued','running','retrying') AND NOT cancel_requested "
            "AND ((params->>'start')::date < $2 OR (params->>'end')::date > $3)",
            account, lower, today,
        )
        await conn.execute(
            "INSERT INTO collector.pending_state(account,cursor_day) VALUES($1,$2) ON CONFLICT DO NOTHING",
            account, lower,
        )
        state = await conn.fetchrow(
            "SELECT * FROM collector.pending_state WHERE account=$1 FOR UPDATE", account,
        )
        now = await conn.fetchval("SELECT clock_timestamp()")
        if state["next_run_at"] > now:
            return None
        active = await conn.fetchval(
            "SELECT id FROM collector.jobs WHERE account=$1 AND mode='pending' "
            "AND status IN ('queued','running','retrying') AND NOT cancel_requested LIMIT 1", account,
        )
        if active:
            return active
        start = max(lower, state["cursor_day"])
        if start > today:
            start = lower
        end = min(today, start + timedelta(days=settings.pending_window_days - 1))
        job = await submit(conn, JobRequest(mode="pending", start=start, end=end),
                           "pending-scheduler", "pending:" + state["next_run_at"].isoformat())
        await conn.execute(
            "UPDATE collector.pending_state SET last_job_id=$2,next_run_at=$3 WHERE account=$1",
            account, job["jobId"], now + timedelta(minutes=settings.pending_interval_minutes),
        )
        return job["jobId"]


# 记录 Pending 扫描覆盖，不把它当作全状态订单的日期采集成功覆盖。
async def commit_pending(conn, job, scope, fetched):
    start = date.fromisoformat(scope["pending_start"])
    end = date.fromisoformat(scope["pending_end"])
    await conn.execute(
        "INSERT INTO collector.pending_coverage(account,start_day,end_day,job_id,fetched) "
        "VALUES($1,$2,$3,$4,$5) ON CONFLICT(account,start_day,end_day) DO UPDATE SET "
        "job_id=excluded.job_id,fetched=excluded.fetched,completed_at=now()",
        job["account"], start, end, job["id"], fetched,
    )
# 只有整个自动扫描任务成功才推进游标；拆分窗口中任一失败都不跳过该范围。
async def finish_pending(conn, job):
    # Advance only after ALL split windows succeed; never skip a failed subwindow.
    if job["actor"] == "pending-scheduler" and not job["params"]["dry_run"]:
        end = date.fromisoformat(job["params"]["end"])
        lower, today = pending_bounds()
        wrapped = end >= today
        cursor = lower if wrapped else max(lower, end + timedelta(days=1))
        await conn.execute(
            "UPDATE collector.pending_state SET cursor_day=$2,last_completed_at=now(),"
            "cycles_completed=cycles_completed+$3 WHERE account=$1 AND last_job_id=$4",
            job["account"], cursor, int(wrapped), job["id"],
        )
