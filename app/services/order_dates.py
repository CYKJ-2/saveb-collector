# 旧数据日期修正：依据来源创建时间分批修复，并保留旧日期、人工修改和修正审计。
"""Evidence-based repair after importing legacy orders; never guess a missing creation date."""
from app.collectors.errors import CollectionError
from app.config.settings import get_settings
from app.domain.orders import SHANGHAI, timestamp
from app.persistence.orders import same
from app.persistence.projections import rebuild


def source_times(raw, source_raw):
    raw = raw if isinstance(raw, dict) else {}
    source = source_raw.get("order") if isinstance(source_raw, dict) else None
    nested = raw.get("order")
    source = source if isinstance(source, dict) else {}
    nested = nested if isinstance(nested, dict) else {}
    values = {}
    created = source.get("createTime") or raw.get("originalCreateTime") or nested.get("createTime")
    if created:
        values["source_created_at"] = timestamp(created)
    for field, aliases in {
        "payment_time": ("paymentTime", "payTime", "paidTime"),
        "completed_time": ("completeTime", "completedTime"),
        "source_updated_at": ("updateTime",),
    }.items():
        value = next((container.get(key) for container in (source, nested, raw) for key in aliases if container.get(key)), None)
        if value:
            values[field] = timestamp(value)
    return values


# 先规划一批修正；apply=True 才写库，写前再次检查版本以保留并发人工编辑。
async def repair_batch(conn, after_id=0, apply=False, batch_size=100):
    account = get_settings().source_account
    counts = {"scanned": 0, "changed": 0, "date_changed": 0, "manual_date_preserved": 0,
              "missing_creation_evidence": 0, "invalid_time": 0, "soft_deleted": 0, "concurrent_skipped": 0, "invoice_skipped": 0}
    async with conn.transaction():
        # Same lock order as collection: account, dates, then business order rows.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "collector-source:" + account)
        records = await conn.fetch(
            "SELECT o.id,o.version,o.classification,o.order_id,o.order_time,o.source_created_at,o.payment_time,o.completed_time,"
            "o.source_updated_at,o.legacy_accounting_time,o.deleted_at,"
            "jsonb_build_object('originalCreateTime',o.raw->'originalCreateTime',"
            "'order',o.raw->'order','paymentTime',o.raw->'paymentTime','payTime',o.raw->'payTime',"
            "'paidTime',o.raw->'paidTime','completeTime',o.raw->'completeTime','completedTime',o.raw->'completedTime',"
            "'updateTime',o.raw->'updateTime') AS raw,"
            "s.raw AS source_raw,s.projected FROM public.orders o "
            "LEFT JOIN collector.source_orders s ON s.order_id=o.order_id AND s.account=$1 "
            "WHERE o.id>$2 ORDER BY o.id LIMIT $3", account, after_id, batch_size,
        )
        plans, days = [], set()
        for record in records:
            counts["scanned"] += 1
            if record["classification"] == "invoice":
                counts["invoice_skipped"] += 1
                continue
            if record["deleted_at"]:
                counts["soft_deleted"] += 1
                continue
            try:
                values = source_times(record["raw"], record["source_raw"])
            except CollectionError:
                counts["invalid_time"] += 1
                continue
            created = values.get("source_created_at")
            if created is None:
                counts["missing_creation_evidence"] += 1
            else:
                previous = record["projected"] or {}
                if "order_time" in previous and not same(record["order_time"], previous["order_time"]):
                    counts["manual_date_preserved"] += 1
                else:
                    values["order_time"] = created
            # Source auxiliary timestamps may fill blanks, but do not erase imported values.
            values = {k: v for k, v in values.items() if k == "order_time" or record[k] is None}
            values = {k: v for k, v in values.items() if not same(record[k], v)}
            if not values:
                continue
            if record["legacy_accounting_time"] is None and record["order_time"]:
                values["legacy_accounting_time"] = record["order_time"]
            counts["changed"] += 1
            counts["date_changed"] += int("order_time" in values)
            for value in (record["order_time"], values.get("order_time")):
                if value:
                    days.add(value.astimezone(SHANGHAI).date())
            plans.append((record, values))
        if apply and plans:
            for day in sorted(days):
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"collector-stats:{day}")
            for record, values in plans:
                current = await conn.fetchrow("SELECT order_time,version,deleted_at FROM public.orders WHERE id=$1 FOR UPDATE", record["id"])
                if current["deleted_at"] or current["version"] != record["version"] or not same(current["order_time"], record["order_time"]):
                    # A concurrent API edit wins; a later run can inspect it again.
                    counts["changed"] -= 1
                    counts["date_changed"] -= int("order_time" in values)
                    counts["concurrent_skipped"] += 1
                    continue
                keys = list(values)
                assignments = ",".join(f'"{key}"=${i + 2}' for i, key in enumerate(keys))
                await conn.execute(f"UPDATE public.orders SET {assignments},version=version+1,updated_at=now() WHERE id=$1",
                                   record["id"], *(values[key] for key in keys))
                if "order_time" in values:
                    summary = {"createTime": values["order_time"].strftime("%y-%m-%d %H:%M"),
                               "accountingTime": values["order_time"].isoformat(), "datePolicy": "source-create-time-v2"}
                    await conn.execute("UPDATE public.orders SET raw=coalesce(raw,'{}'::jsonb)||$2::jsonb WHERE id=$1", record["id"], summary)
                # Update the expected projection baseline, so this repair is not mistaken for a manual edit.
                if record["projected"] is not None:
                    await conn.execute("UPDATE collector.source_orders SET projected=projected||$3::jsonb WHERE account=$1 AND order_id=$2",
                                       account, record["order_id"], values)
                source_created = source_times(record["raw"], record["source_raw"]).get("source_created_at")
                if source_created and record["source_raw"]:
                    await conn.execute(
                        "UPDATE collector.source_orders SET normalized=jsonb_set(jsonb_set(normalized,"
                        "'{day}',$3::jsonb),'{columns}',(normalized->'columns')||$4::jsonb) "
                        "WHERE account=$1 AND order_id=$2", account, record["order_id"],
                        source_created.astimezone(SHANGHAI).date().isoformat(),
                        {"order_time": source_created.isoformat(), "source_created_at": source_created.isoformat()},
                    )
                await conn.execute("INSERT INTO collector.order_date_repairs(order_id,before_value,after_value) VALUES($1,$2,$3)",
                                   record["order_id"], {k: record[k] for k in values}, values)
            await rebuild(conn, days, "order-date-repair")
    return {"after_id": records[-1]["id"] if records else after_id, **counts}
