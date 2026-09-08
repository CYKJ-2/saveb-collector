# 统计投影：从实际生效的业务订单重建受影响日期的日统计及旧首页兼容快照。
"""Rebuild only affected non-invoice days, with compatible snapshots in the same transaction."""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from app.domain.orders import CATEGORIES, SHANGHAI, digest
from app.persistence.db import encode


# 读取业务表最终值而非来源原值，确保统计反映人工调整；调用方持有受影响日期锁。
async def rebuild(conn, days, job_id):
    for day in sorted(days):
        # Shared daily lock serializes collectors; ERP manual tables stay independently owned.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"collector-stats:{day}"
        )
        await conn.execute("DELETE FROM daily_stats WHERE stat_date=$1 AND channel<>'invoice'", day)
        await conn.execute(
            """INSERT INTO daily_stats(stat_date,channel,staff_code,orders_count,items_count,usd_amount)
            SELECT $1,coalesce(classification,'unmatched'),'',count(*)::int,
                   coalesce(sum(items_count),0)::int,coalesce(sum(amount_usd),0)
            FROM orders WHERE (order_time AT TIME ZONE 'Asia/Shanghai')::date=$1
            AND deleted_at IS NULL AND lower(order_status)='completed'
            AND coalesce(classification,'unmatched')<>'invoice' GROUP BY classification""",
            day,
        )
        records = await conn.fetch(
            "SELECT * FROM orders WHERE (order_time AT TIME ZONE 'Asia/Shanghai')::date=$1 AND deleted_at IS NULL AND coalesce(classification,'unmatched')<>'invoice' ORDER BY order_time DESC,order_id",
            day,
        )
        rows, pending, testing = [], [], []
        buckets = {}
        statuses = {}
        staff_buckets = {}
        for r in records:
            state = str(r["order_status"] or "").lower()
            label = CATEGORIES.get(
                r["classification"],
                "Offline Orders" if r["classification"] == "payment_link" else "Unmatched",
            )
            summary = {
                **(r["raw"] or {}),
                "orderId": r["order_id"],
                "clientOrderId": r.get("client_order_id"),
                "category": label,
                "sourceCategory": label,
                "amount": float(r["amount_original"] or 0),
                "currency": r["currency"],
                "items": r["items_count"],
                "staff": r["staff_code"],
                "topInfluencer": r["influencer_name"],
                "customerFullName": r["customer_name"],
                "clientSite": r["source_site"],
                "recipientPaypal": r["receiving_paypal"],
                "createTime": r["order_time"].astimezone(SHANGHAI).strftime("%y-%m-%d %H:%M"),
                "paymentStatus": state.title(),
            }
            statuses[state.title()] = statuses.get(state.title(), 0) + 1
            if state == "testing":
                testing.append(summary)
            elif state == "completed":
                rows.append(summary)
                b = buckets.setdefault(
                    label,
                    {"name": label, "orders": 0, "items": 0, "USD": Decimal(0), "EUR": 0, "GBP": 0},
                )
                b["orders"] += 1
                b["items"] += r["items_count"] or 0
                b["USD"] += r["amount_usd"] or 0
                if r["staff_code"]:
                    parts = (r["raw"] or {}).get("staffAllocations") or []
                    names = {str(p.get("staff") or "") for p in parts}
                    # A manual reassignment of staff_code takes precedence over old allocations.
                    if names != {s.strip() for s in r["staff_code"].split(",")}:
                        parts = []
                    parts = [
                        p for p in parts if p.get("staff") and Decimal(str(p.get("items") or 0)) > 0
                    ]
                    if not parts:
                        parts = [{"staff": r["staff_code"], "items": r["items_count"] or 1}]
                    total_items = sum(Decimal(str(p["items"])) for p in parts)
                    remaining = r["amount_usd"] or Decimal(0)
                    for index, part in enumerate(parts):
                        weight = Decimal(str(part["items"]))
                        share = (
                            remaining
                            if index == len(parts) - 1
                            else ((r["amount_usd"] or 0) * weight / total_items).quantize(
                                Decimal(".01"), rounding=ROUND_HALF_UP
                            )
                        )
                        remaining -= share
                        target = staff_buckets.setdefault(
                            (r["classification"] or "unmatched", part["staff"]),
                            {"orders": 0, "items": Decimal(0), "usd": Decimal(0)},
                        )
                        target["orders"] += 1
                        target["items"] += weight
                        target["usd"] += share
            else:
                pending.append(summary)
        for (channel, staff), bucket in staff_buckets.items():
            await conn.execute(
                "INSERT INTO daily_stats(stat_date,channel,staff_code,orders_count,items_count,usd_amount) VALUES($1,$2,$3,$4,$5,$6)",
                day,
                channel,
                staff,
                bucket["orders"],
                int(bucket["items"].quantize(Decimal(1), rounding=ROUND_HALF_UP)),
                bucket["usd"],
            )
        staff_rows = await conn.fetch(
            "SELECT staff_code AS name, sum(orders_count)::int AS orders,sum(items_count)::int AS items,sum(usd_amount) AS \"USD\" FROM daily_stats WHERE stat_date=$1 AND channel<>'invoice' AND staff_code<>'' GROUP BY staff_code",
            day,
        )
        now = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
        payload = {
            "ok": True,
            "date": str(day),
            "startDate": str(day),
            "endDate": str(day),
            "generatedAt": now,
            "collectorJobId": job_id,
            "orders": rows,
            "recent": rows[:30],
            "pendingOrders": pending,
            "pendingRecent": pending[:30],
            "testingOrders": testing,
            "testingRecent": testing[:30],
            "category": list(buckets.values()),
            "staff": [dict(r) for r in staff_rows],
            "paymentStatus": statuses,
            "totalFetchedOrders": len(records),
            "totals": {
                "orders": len(rows),
                "items": sum(r["items"] or 0 for r in rows),
                "USD": sum((b["USD"] for b in buckets.values()), Decimal(0)),
                "EUR": 0,
                "GBP": 0,
            },
        }
        await conn.execute(
            """INSERT INTO legacy_dashboard_days(day,payload,source_sha256,source_size_bytes,snapshot_cutoff_asia_shanghai)
            VALUES($1,$2,$3,$4,$5) ON CONFLICT(day) DO UPDATE SET payload=excluded.payload,
            source_sha256=excluded.source_sha256,source_size_bytes=excluded.source_size_bytes,
            snapshot_cutoff_asia_shanghai=excluded.snapshot_cutoff_asia_shanghai,updated_at=now()""",
            day,
            payload,
            digest(payload),
            len(encode(payload).encode()),
            now,
        )
        await conn.execute(
            """INSERT INTO system_state(key,value) VALUES('legacy_dashboard_latest',$1)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()
            WHERE coalesce(system_state.value->'payload'->>'date','') <= $2""",
            {
                "payload": payload,
                "sourceSha256": digest(payload),
                "snapshotCutoffAsiaShanghai": now,
            },
            str(day),
        )
