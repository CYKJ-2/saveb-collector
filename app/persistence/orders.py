# 订单持久化：来源快照与业务表分开保存，通过三方比较保护人工字段及软删除记录。
"""Source storage and three-way merge: never overwrite a field edited since our last projection."""

from datetime import datetime
from decimal import Decimal

from app.collectors.dh_order import CollectionError
from app.domain.orders import SHANGHAI, digest, timestamp

DECIMALS = {"amount_original", "amount_usd"}
TIMESTAMPS = {"order_time", "source_created_at", "payment_time", "completed_time", "source_updated_at", "legacy_accounting_time"}

PROTECTED_INITIAL = {"classification", "staff_code", "influencer_name"}


# 版本比较忽略来源备注和图片地址等独立展示字段，业务字段仍参与同版本冲突检查。
def source_revision_payload(raw):
    """Ignore only independently edited annotations when checking source revisions.

    DH-Order changes remarks and product image URLs without advancing updateTime.
    These presentation fields remain in archived/source raw; business fields and
    product identity/quantity stay guarded.
    """
    value = {key: item for key, item in raw.items() if not key.startswith("_collector_")}
    if isinstance(value.get("order"), dict):
        value["order"] = {key: item for key, item in value["order"].items() if key != "remark"}
    if isinstance(value.get("productList"), list):
        value["productList"] = [
            {key: item for key, item in product.items() if key != "productImg"}
            if isinstance(product, dict)
            else product
            for product in value["productList"]
        ]
    return value


def same(left, right):

    if left is None or right is None:
        return left is right

    if isinstance(left, datetime):
        return left == timestamp(right)

    if isinstance(left, Decimal):
        return left == Decimal(str(right))

    return str(left) == str(right)


# current 是业务现值，previous 是上次采集写入基线，incoming 是本次来源值。
# 现值偏离基线时视为人工调整，保留现值；返回可写字段和受保护字段列表。
def merge_columns(current, previous, incoming):

    accepted = {}

    protected = []

    for key, value in incoming.items():
        if current is not None and (
            (previous is None and key in PROTECTED_INITIAL)
            or (
                previous is not None
                and key in previous
                and not same(current.get(key), previous[key])
            )
        ):
            protected.append(key)

        else:
            accepted[key] = value

    return accepted, protected


# 调用方负责事务和日期统计锁；返回处理状态及需要重建统计的日期集合。
# publish=False 仅保存来源层，发布业务表时仍遵守人工修改与软删除保护。
async def upsert(conn, account, normalized, raw, job_id, publish):

    key = normalized["id"]

    prior = await conn.fetchrow(
        "SELECT * FROM collector.source_orders WHERE account=$1 AND order_id=$2 FOR UPDATE",
        account,
        key,
    )

    source_time = timestamp(normalized["source_updated_at"])

    # Source JSON includes calculated values/context. A changed context must be traceable too.

    content_hash = digest({"raw": raw, "context": normalized["context_version"]})

    if (
        prior
        and source_time
        and prior["source_updated_at"]
        and source_time < prior["source_updated_at"]
    ):
        return "stale", set()

    # 相同来源版本却改变业务内容时拒绝覆盖；完全相同的来源数据允许正常成功。
    if (
        prior
        and source_time
        and prior["source_updated_at"] == source_time
        and digest(source_revision_payload(prior["raw"])) != digest(source_revision_payload(raw))
    ):
        raise CollectionError("SOURCE_VERSION_CONFLICT")

    unchanged = prior and prior["content_hash"] == content_hash

    days = set()

    projected = prior["projected"] if prior else None

    if prior and all(
        prior["normalized"]["columns"].get(k) == normalized["columns"].get(k)
        for k in ("amount_original", "currency")
    ):
        # A new rate download does not silently revalue previously recorded revenue.

        normalized["columns"]["amount_usd"] = prior["normalized"]["columns"]["amount_usd"]

        normalized["fx_rate"] = prior["normalized"].get("fx_rate")

    if publish:
        # ERP has a global order_id identity. Refuse to bind two accounts to it.

        collision = await conn.fetchval(
            "SELECT 1 FROM collector.source_orders WHERE account<>$1 AND order_id=$2 AND projected IS NOT NULL",
            account,
            key,
        )

        if collision:
            raise CollectionError("API_ORDER_IDENTITY_COLLISION")

        current_row = await conn.fetchrow("SELECT * FROM orders WHERE order_id=$1 FOR UPDATE", key)

        current = dict(current_row) if current_row else None

        if current and current.get("order_time"):
            days.add(current["order_time"].astimezone(SHANGHAI).date())

        if current and current.get("deleted_at"):
            return "stale", set()  # 人工软删除的订单不复活、不更新。

        incoming = dict(normalized["columns"])
        if current and current.get("legacy_accounting_time") is None:
            incoming["legacy_accounting_time"] = current.get("order_time").isoformat() if current.get("order_time") else None

        if normalized["testing"]:
            incoming["order_status"] = "testing"

        accepted, protected = merge_columns(current, projected, incoming)

        values = []

        keys = list(accepted)

        for field in keys:
            value = accepted[field]

            if field in TIMESTAMPS:
                value = timestamp(value)

            elif field in DECIMALS and value is not None:
                value = Decimal(str(value))

            values.append(value)

        # Preserve raw fields owned by ERP; replace source summary keys only if unmodified.

        current_raw = current.get("raw") or {} if current else {}

        previous_summary = prior["normalized"]["summary"] if prior and projected else None

        summary = dict(current_raw)

        for field, value in normalized["summary"].items():
            if (
                previous_summary
                and field in previous_summary
                and current_raw.get(field) != previous_summary[field]
            ):
                continue

            summary[field] = value

        # Ensure compatibility raw mirrors actual effective canonical columns.

        effective = {**(current or {}), **accepted}
        effective_date = timestamp(effective.get("order_time"))
        if effective_date:
            summary["createTime"] = effective_date.strftime("%y-%m-%d %H:%M")
            summary["accountingTime"] = effective_date.isoformat()

        for col, field in {
            "staff_code": "staff",
            "influencer_name": "topInfluencer",
            "amount_original": "amount",
            "currency": "currency",
            "items_count": "items",
            "customer_name": "customerFullName",
            "receiving_paypal": "recipientPaypal",
            "source_site": "clientSite",
            "order_status": "paymentStatus",
        }.items():
            if col in effective:
                summary[field] = effective[col]

        if current:
            assignments = ",".join(f'"{field}"=${i + 2}' for i, field in enumerate(keys))

            prefix = assignments + "," if assignments else ""

            await conn.execute(
                f"UPDATE orders SET {prefix}raw=${len(values) + 2},version=version+1,updated_at=now() WHERE order_id=$1",
                key,
                *values,
                summary,
            )

        else:
            columns = ",".join(f'"{k}"' for k in keys)

            placeholders = ",".join(f"${i + 2}" for i in range(len(keys)))

            await conn.execute(
                f"INSERT INTO orders(order_id,{columns},raw) VALUES($1,{placeholders},${len(values) + 2})",
                key,
                *values,
                summary,
            )

        effective_time = timestamp(effective.get("order_time"))

        if effective_time:
            days.add(effective_time.date())

        # Keep old expected values for protected fields so they remain protected next run.

        # 受保护字段保持旧的期望基线，下一次采集才能继续识别为人工调整。
        projected = {**(projected or {}), **accepted}

        for field in protected:
            if field not in projected:
                projected[field] = incoming[field]

    await conn.execute(
        """INSERT INTO collector.source_orders(account,order_id,normalized,raw,content_hash,source_updated_at,job_id,projected)

        VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(account,order_id) DO UPDATE SET

        normalized=excluded.normalized,raw=excluded.raw,content_hash=excluded.content_hash,

        source_updated_at=excluded.source_updated_at,last_seen_at=now(),job_id=excluded.job_id,projected=excluded.projected""",
        account,
        key,
        normalized,
        raw,
        content_hash,
        source_time,
        job_id,
        projected,
    )

    if not unchanged:
        await conn.execute(
            "INSERT INTO collector.changes(job_id,order_id,before_value,after_value) VALUES($1,$2,$3,$4)",
            job_id,
            key,
            prior["normalized"] if prior else None,
            normalized,
        )

    return "unchanged" if unchanged else "updated" if prior else "inserted", days
