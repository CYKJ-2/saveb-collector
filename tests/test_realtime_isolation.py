"""大量历史 Pending 不能阻止新日期采集；旧任务升级不丢进度或业务数据。"""
from datetime import datetime, timedelta
from pathlib import Path

from conftest import FakeClient, order
from test_pipeline import execute

from app.domain.jobs import JobRequest
from app.domain.orders import SHANGHAI
from app.queue.tasks import check_schedule
from app.services.collection import run_chunk
from app.services.jobs import status, submit


async def test_thousands_of_open_orders_do_not_block_realtime_or_next_cycle(db):
    await db.execute("""INSERT INTO collector.source_orders
        (account,order_id,normalized,raw,content_hash,job_id)
        SELECT 'default','open-'||n,'{"columns":{"order_status":"pending"}}',
               '{}','fixture','fixture' FROM generate_series(1,3286) n""")
    today = datetime.now(SHANGHAI).date()
    job = await submit(db, JobRequest(), "scheduler", "first")
    assert job["total"] == 2
    assert [c["scope"] for c in job["chunks"]] == [
        {"day": today.isoformat()}, {"day": (today - timedelta(days=1)).isoformat()},
    ]
    assert job["params"]["start"] == (today - timedelta(days=1)).isoformat()
    assert job["params"]["end"] == today.isoformat()
    background = job["params"]["background_job_id"]
    assert (await status(db, background))["total"] >= 3286
    # 历史队列在网络抓取前让出优先权；即时任务不等这三千多个分片。
    class MustYield(FakeClient):
        async def collect(self, scope):
            raise AssertionError("history must yield to realtime")
    await run_chunk(db, background, MustYield)
    assert await db.fetchval("SELECT sum(attempts) FROM collector.chunks WHERE job_id=$1", background) == 0
    for _ in range(2):
        await run_chunk(db, job["jobId"], FakeClient)
    assert (await status(db, job["jobId"]))["status"] == "succeeded"
    await run_chunk(db, background, FakeClient)
    assert (await status(db, background))["completed"] == 1
    await db.execute("INSERT INTO collector.schedules(account,next_run_at) VALUES('default',now()-interval '1 minute')")
    next_id = await check_schedule()
    assert next_id and next_id != job["jobId"]
    assert (await status(db, next_id))["total"] == 2
    assert (await status(db, next_id))["params"]["background_job_id"] == background


async def test_previous_day_refresh_does_not_suppress_today(db):
    yesterday = datetime.now(SHANGHAI).date() - timedelta(days=1)
    old = await submit(db, JobRequest(start=yesterday, end=yesterday), "scheduler", "yesterday")
    await db.execute("INSERT INTO collector.schedules(account,next_run_at) VALUES('default',now()-interval '1 minute')")
    fresh = await check_schedule()
    assert fresh and fresh != old["jobId"]
    assert (await status(db, fresh))["params"]["end"] == (yesterday + timedelta(days=1)).isoformat()


async def test_upgrade_preserves_old_chunks_and_wakes_schedule(db):
    baseline = await execute(db, [order(status="Pending")], "baseline")
    await db.execute("UPDATE orders SET customer_name='Manual name'")
    # 模拟旧刷新：一个已入库分片和一个尚未执行的历史订单分片。
    old_id = baseline["jobId"]
    await db.execute("UPDATE collector.jobs SET mode='refresh',status='running' WHERE id=$1", old_id)
    await db.execute("INSERT INTO collector.chunks(job_id,scope) VALUES($1,'{\"order_id\":\"A\"}')", old_id)
    await db.execute("INSERT INTO collector.outbox(job_id) VALUES($1)", old_id)
    await db.execute("INSERT INTO collector.schedules(account,next_run_at) VALUES('default',now()+interval '30 minutes')")
    before = await db.fetch("SELECT * FROM collector.chunks WHERE job_id=$1 ORDER BY id", old_id)
    sql = (Path(__file__).parents[1] / "migrations/006_separate_realtime.sql").read_text(encoding="utf-8")
    await db.execute(sql)
    migrated = await status(db, old_id)
    assert migrated["mode"] == "history" and migrated["actor"] == "test"
    assert migrated["params"]["task_kind"] == "open_recheck"
    assert before == await db.fetch("SELECT * FROM collector.chunks WHERE job_id=$1 ORDER BY id", old_id)
    assert await db.fetchval("SELECT customer_name FROM orders") == "Manual name"
    assert await db.fetchval("SELECT count(*) FROM orders") == 1
    due = await db.fetchval("SELECT next_run_at FROM collector.schedules")
    await db.execute(sql)
    assert due == await db.fetchval("SELECT next_run_at FROM collector.schedules")
    fresh = await check_schedule()
    assert fresh and fresh != old_id
    # 两个近期日期采集完成后，历史单号继续复查 Pending -> Completed。
    for _ in range(2):
        await run_chunk(db, fresh, FakeClient)
    client = type("Completed", (FakeClient,), {"rows": [order(updated="26-09-02 10:00")]})
    await run_chunk(db, old_id, client)
    assert (await status(db, old_id))["status"] == "succeeded"
    assert await db.fetchval("SELECT order_status FROM orders") == "completed"
    assert await db.fetchval("SELECT customer_name FROM orders") == "Manual name"
    assert (await db.fetchval("SELECT stat_date FROM daily_stats")).isoformat() == "2026-09-01"
