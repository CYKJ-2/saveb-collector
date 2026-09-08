# 采集对账：核对来源关联、业务订单、日统计和兼容快照；仅保存报告，不自动修复业务数据。
"""Read-only business reconciliation; persist findings separately from job success."""
from datetime import date
from decimal import Decimal

from app.config.settings import get_settings


def totals(row):
    return (int(row["orders"] or 0), int(row["items"] or 0), Decimal(str(row["usd"] or 0)))


# 以保留人工调整后的业务表为统计基准；人工修改和软删除单独计数，不直接当作对账失败。
async def reconcile(conn, start, end, *, job_id=None, chunk_id=None, order_ids=None, checked_days=None):
    account = get_settings().source_account
    # The caller supplies a repeatable-read snapshot (CLI/daily) or the publication transaction.
    expected = await conn.fetch(
        "SELECT (order_time AT TIME ZONE 'Asia/Shanghai')::date AS day,"
        "coalesce(classification,'unmatched') AS channel,count(*) AS orders,"
        "sum(items_count) AS items,sum(amount_usd) AS usd FROM public.orders "
        "WHERE deleted_at IS NULL AND lower(order_status)='completed' AND coalesce(classification,'unmatched')<>'invoice' "
        "AND (order_time AT TIME ZONE 'Asia/Shanghai')::date BETWEEN $1 AND $2 "
        "AND ($3::date[] IS NULL OR (order_time AT TIME ZONE 'Asia/Shanghai')::date=ANY($3)) "
        "GROUP BY 1,2", start, end, checked_days,
    )
    actual = await conn.fetch(
        "SELECT stat_date AS day,channel,sum(orders_count) AS orders,"
        "sum(items_count) AS items,sum(usd_amount) AS usd FROM public.daily_stats "
        "WHERE stat_date BETWEEN $1 AND $2 AND staff_code='' AND channel<>'invoice' "
        "AND ($3::date[] IS NULL OR stat_date=ANY($3)) GROUP BY 1,2",
        start, end, checked_days,
    )
    left = {(r["day"], r["channel"]): totals(r) for r in expected}
    right = {(r["day"], r["channel"]): totals(r) for r in actual}
    differences = [
        {"day": str(k[0]), "channel": k[1], "orders": left.get(k, (0, 0, Decimal(0))),
         "daily_stats": right.get(k, (0, 0, Decimal(0)))}
        for k in sorted(left.keys() | right.keys()) if left.get(k, (0, 0, Decimal(0))) != right.get(k, (0, 0, Decimal(0)))
    ]
    snapshots = {r["day"]: r["payload"] for r in await conn.fetch(
        "SELECT day,payload FROM public.legacy_dashboard_days WHERE day BETWEEN $1 AND $2 "
        "AND ($3::date[] IS NULL OR day=ANY($3))", start, end, checked_days,
    )}
    # Also check empty / pending-only days and retained snapshots after deletion.
    days = {r["day"] for r in await conn.fetch(
        "SELECT DISTINCT (order_time AT TIME ZONE 'Asia/Shanghai')::date AS day FROM public.orders "
        "WHERE coalesce(classification,'unmatched')<>'invoice' AND (order_time AT TIME ZONE 'Asia/Shanghai')::date BETWEEN $1 AND $2 "
        "AND ($3::date[] IS NULL OR (order_time AT TIME ZONE 'Asia/Shanghai')::date=ANY($3))", start, end, checked_days,
    )} | set(snapshots)
    snapshot_differences = []
    for day in sorted(days):
        values = [v for (d, _), v in left.items() if d == day]
        wanted = tuple(sum(v[i] for v in values) for i in range(3))
        payload = snapshots.get(day)
        got = payload.get("totals", {}) if isinstance(payload, dict) else {}
        try:
            matches = (int(got["orders"]), int(got["items"]), Decimal(str(got["USD"]))) == wanted
        except (KeyError, TypeError, ValueError, ArithmeticError):
            matches = False
        if not matches:
            snapshot_differences.append({"day": str(day), "expected": wanted, "snapshot": got})
    source = await conn.fetchrow(
        "SELECT count(*) AS source_count,"
        "count(*) FILTER(WHERE s.projected IS NOT NULL AND o.order_id IS NULL) AS missing_orders,"
        "count(*) FILTER(WHERE o.deleted_at IS NOT NULL) AS soft_deleted,"
        "count(*) FILTER(WHERE o.deleted_at IS NULL AND s.projected IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM jsonb_each_text(s.projected) p WHERE CASE "
        "WHEN p.key IN ('amount_original','amount_usd','items_count') THEN "
        "((to_jsonb(o)->>p.key)::numeric IS DISTINCT FROM p.value::numeric) "
        "WHEN p.key IN ('order_time','source_created_at','payment_time','completed_time','source_updated_at','legacy_accounting_time') THEN "
        "((to_jsonb(o)->>p.key)::timestamptz IS DISTINCT FROM p.value::timestamptz) "
        "ELSE (to_jsonb(o)->>p.key) IS DISTINCT FROM p.value END)) AS manually_adjusted "
        "FROM collector.source_orders s LEFT JOIN public.orders o ON o.order_id=s.order_id "
        "WHERE s.account=$1 AND (($4::text[] IS NOT NULL AND s.order_id=ANY($4)) OR "
        "($4::text[] IS NULL AND (s.normalized->>'day')::date BETWEEN $2 AND $3))",
        account, start, end, order_ids,
    )
    unarchived = 0
    if order_ids:
        # A soft-deleted order is deliberately skipped by upsert, including source storage.
        unarchived = await conn.fetchval(
            "SELECT count(*) FROM unnest($2::text[]) AS x(order_id) WHERE NOT EXISTS "
            "(SELECT 1 FROM collector.source_orders s WHERE s.account=$1 AND s.order_id=x.order_id) "
            "AND NOT EXISTS (SELECT 1 FROM public.orders o WHERE o.order_id=x.order_id AND o.deleted_at IS NOT NULL)",
            account, order_ids,
        )
    report = {
        "checked_days": checked_days,
        "scope": "source linkage; effective business orders vs channel totals and dashboard totals",
        "source": dict(source), "unarchived_selected": unarchived,
        "stats_difference_count": len(differences), "stats_differences": differences[:100],
        "snapshot_difference_count": len(snapshot_differences), "snapshot_differences": snapshot_differences[:100],
        "manual_adjustments_are_errors": False,
    }
    state = "warning" if differences or snapshot_differences or source["missing_orders"] or unarchived else "passed"
    report_id = await conn.fetchval(
        "INSERT INTO collector.reconciliation_reports(account,job_id,chunk_id,kind,start_day,end_day,status,report) "
        "VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
        account, job_id, chunk_id, "chunk" if chunk_id else "range", start, end, state, report,
    )
    return {"id": report_id, "status": state, "start": start, "end": end, **report}


async def daily_reconcile():
    from datetime import datetime

    from app.domain.orders import SHANGHAI
    from app.persistence.db import connect

    if not get_settings().publish_api:
        return None
    async with connect() as conn, conn.transaction(isolation="repeatable_read"):
        # Avoid duplicate daily runs on accidentally duplicated Beat deliveries.
        if not await conn.fetchval("SELECT pg_try_advisory_xact_lock(825724)"):
            return None
        return await reconcile(conn, date.fromisoformat(get_settings().history_start), datetime.now(SHANGHAI).date())
