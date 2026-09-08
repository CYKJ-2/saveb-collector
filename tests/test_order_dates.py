from datetime import date

from conftest import order
from test_pipeline import execute

from app.domain.orders import SHANGHAI
from app.services.order_dates import repair_batch, source_times


async def test_pending_payment_and_later_update_keep_creation_day(db):
    pending = order(status="Pending")
    await execute(db, [pending], "pending")
    completed = order(updated="26-09-03 10:00")
    completed["order"].update(paymentTime="26-09-02 23:55", completedTime="26-09-03 00:01")
    result = await execute(db, [completed], "completed")
    assert result["status"] == "succeeded"
    row = await db.fetchrow("SELECT * FROM orders")
    assert row["order_time"].date() == date(2026, 9, 1)
    assert row["source_created_at"] == row["order_time"]
    assert row["payment_time"].date() == date(2026, 9, 2)
    assert row["completed_time"].astimezone(SHANGHAI).date() == date(2026, 9, 3)
    assert row["source_updated_at"].date() == date(2026, 9, 3)
    assert await db.fetchval("SELECT stat_date FROM daily_stats") == date(2026, 9, 1)
    assert await db.fetchval("SELECT count(*) FROM orders") == 1


async def test_legacy_import_without_new_fields_and_idempotent_repair(db):
    # Equivalent to an old --column-inserts dump: all new columns omitted.
    await db.execute("INSERT INTO orders(order_id,order_time,order_status,items_count,amount_usd,raw) "
                     "VALUES('legacy','2026-09-03 10:00+08','completed',2,12.3,$1)",
                     {"createTime": "26-09-03 10:00", "originalCreateTime": "26-09-01 09:00", "paymentTime": "26-09-03 09:59"})
    preview = await repair_batch(db)
    assert preview["date_changed"] == 1
    assert await db.fetchval("SELECT extract(day from order_time)::int FROM orders") == 3
    assert await db.fetchval("SELECT count(*) FROM collector.order_date_repairs") == 0
    result = await repair_batch(db, apply=True)
    assert result["date_changed"] == 1
    row = await db.fetchrow("SELECT * FROM orders")
    assert row["order_time"].date() == date(2026, 9, 1)
    assert row["legacy_accounting_time"].date() == date(2026, 9, 3)
    assert row["payment_time"].date() == date(2026, 9, 3)
    assert await db.fetchval("SELECT stat_date FROM daily_stats") == date(2026, 9, 1)
    assert (await repair_batch(db, apply=True))["changed"] == 0


async def test_unknown_legacy_creation_and_soft_delete_are_retained(db):
    await db.execute("INSERT INTO orders(order_id,order_time,raw) VALUES('unknown','2026-09-03 10:00+08',$1)", {"createTime": "26-09-03 10:00"})
    result = await repair_batch(db, apply=True)
    assert result["missing_creation_evidence"] == 1
    assert result["date_changed"] == 0
    await db.execute("UPDATE orders SET deleted_at=now(),raw=$1", {"originalCreateTime": "26-09-01 09:00"})
    assert (await repair_batch(db, apply=True))["soft_deleted"] == 1
    assert await db.fetchval("SELECT extract(day from order_time)::int FROM orders") == 3


async def test_manual_date_survives_repair_and_next_collection(db):
    await execute(db, [order()], "initial")
    await db.execute("UPDATE orders SET order_time='2026-08-30 10:00+08'")
    result = await repair_batch(db, apply=True)
    assert result["manual_date_preserved"] == 1
    await execute(db, [order(updated="26-09-03 10:00")], "refresh")
    assert await db.fetchval("SELECT extract(day from order_time)::int FROM orders") == 30


def test_legacy_ambiguous_display_time_is_not_creation_evidence():
    assert source_times({"createTime": "26-09-03 10:00"}, None) == {}
    result = source_times({"originalCreateTime": "26-09-01 09:00"}, None)
    assert result["source_created_at"].date() == date(2026, 9, 1)
    assert source_times({"order": "legacy-invalid-shape"}, None) == {}


async def test_legacy_invoice_mirror_does_not_overwrite_invoice_statistics(db):
    await db.execute("INSERT INTO orders(order_id,order_time,classification,order_status,amount_usd,items_count,raw) "
                     "VALUES('invoice-mirror','2026-09-01 10:00+08','invoice','completed',50,1,$1)",
                     {"originalCreateTime": "26-08-30 09:00"})
    await db.execute("INSERT INTO daily_stats(stat_date,channel,staff_code,orders_count,items_count,usd_amount) VALUES('2026-09-01','invoice','',1,1,50)")
    result = await execute(db, [order()], "source")
    assert result["status"] == "succeeded"
    assert await db.fetchval("SELECT usd_amount FROM daily_stats WHERE channel='invoice'") == 50
    assert (await repair_batch(db, apply=True))["invoice_skipped"] == 1
    assert await db.fetchval("SELECT extract(day from order_time)::int FROM orders WHERE order_id='invoice-mirror'") == 1
