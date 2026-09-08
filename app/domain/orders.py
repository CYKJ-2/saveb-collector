# 订单标准化：将 DH 响应映射为业务字段和兼容摘要；此层不直接写数据库。
import hashlib
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app.collectors.dh_order import CollectionError
from app.persistence.db import encode

SHANGHAI = ZoneInfo("Asia/Shanghai")
TIME_KEYS = (
    "createTime",
    "paymentTime",
    "payTime",
    "paidTime",
    "completeTime",
    "completedTime",
    "updateTime",
)
CATEGORIES = {
    "official": "Official Sites",
    "top_influencer": "Top Influencers",
    "mid_influencer": "Mid Influencers",
    "offline": "Offline Orders",
    "unmatched": "Unmatched",
}


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def timestamp(value):
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%y-%m-%d %H:%M", "%y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=SHANGHAI)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(value)
        return (
            parsed.replace(tzinfo=SHANGHAI)
            if parsed.tzinfo is None
            else parsed.astimezone(SHANGHAI)
        )
    except ValueError:
        raise CollectionError("ORDER_TIMESTAMP_INVALID") from None


def money(value):
    try:
        amount = Decimal(str(value).replace(",", ""))
        if not amount.is_finite():
            raise InvalidOperation
        return amount
    except (InvalidOperation, ValueError):
        raise CollectionError("ORDER_AMOUNT_INVALID") from None


def domain(value):
    value = str(value or "").strip().lower()
    return (
        (urlsplit(value if "://" in value else "https://" + value).hostname or "")
        .removeprefix("www.")
        .rstrip(".")
    )


# 按冻结的站点和员工规则识别订单渠道、达人及线下员工分摊。
def classify(site, names, context):
    rules = context["rules"]
    host = domain(site)
    codes = sorted({*rules.get("staffCodes", []), "XY"}, key=lambda c: (-len(c), c))
    text = "\n".join(names).upper()
    allocations = {}
    if codes:
        pattern = (
            r"(?<![A-Z0-9])("
            + "|".join(re.escape(c) for c in codes)
            + r")\s*[-:]?\s*(\d+)(?![A-Z0-9])"
        )
        for staff, count in re.findall(pattern, text):
            if 0 < int(count) <= 99:
                allocations[staff] = allocations.get(staff, 0) + int(count)
    staff = ", ".join(allocations) or next((code for code in codes if code in text), "")
    name = rules.get("topInfluencerMap", {}).get(host, "")
    if name.lower() in ("david", "david coey"):
        name = "David Coey"
    authority = rules.get("authority", {}).get(host)
    if authority:
        category = authority
    elif host in (
        "saveb.link",
        "saveb-link.net",
        "saveb-link.com",
        "saveb-link-lux.com",
    ) or host.startswith(("saveb-link.", "saveb-link-")):
        category = "offline"
    elif host in ("saveb-co.com", "saveb-my.co"):
        category = "official"
    else:
        raw = rules.get("domainMap", {}).get(host, "")
        category = {
            "主站": "official",
            "主站群组": "official",
            "Main Group": "official",
            "Main Sites": "official",
            "Official Sites": "official",
            "线下订单": "offline",
            "Offline Orders": "offline",
            "网红A组": "top_influencer",
            "头部网红": "top_influencer",
            "Top Influencers": "top_influencer",
            "网红B组": "mid_influencer",
            "腰部网红": "mid_influencer",
            "Mid Influencers": "mid_influencer",
        }.get(raw, "unmatched")
        if name or category in ("top_influencer", "mid_influencer"):
            category = (
                "top_influencer"
                if name and rules.get("topInfluencerTierMap", {}).get(name) == "top"
                else "mid_influencer"
            )
    return (
        category,
        name if category in ("top_influencer", "mid_influencer") else "",
        staff if category == "offline" else "",
        allocations if category == "offline" else {},
    )


# 以 createTime 固定订单归属日期；付款、完成和来源更新时间分别保存，避免跨日完成挪走原日期。
def normalize(entry, context):
    order = entry["order"]
    key = str(order.get("orderId") or "").strip()
    if not key:
        raise CollectionError("ORDER_ID_MISSING")
    # Required source fields cannot silently become zero or an empty status.
    for field in ("amount", "currency", "paymentStatus", "createTime"):
        if order.get(field) in (None, ""):
            raise CollectionError("ORDER_REQUIRED_FIELD_MISSING")
    times = [timestamp(order.get(k)) for k in TIME_KEYS if order.get(k)]
    accounting = timestamp(order["createTime"])
    products = entry.get("productList")
    if not isinstance(products, list):
        raise CollectionError("PRODUCT_LIST_MISSING")
    names = [str(p.get("productName") or "") for p in products]
    category, influencer, staff, allocations = classify(order.get("clientSite"), names, context)
    quantities = [money(p.get("number") or 1) for p in products]
    if any(n <= 0 or n != n.to_integral_value() for n in quantities):
        raise CollectionError("PRODUCT_QUANTITY_INVALID")
    items = sum(quantities) or Decimal(1)
    raw_items = items
    if category == "offline" and sum(entry.get("_collector_image_counts") or []):
        items = Decimal(sum(entry["_collector_image_counts"]))
    if allocations:
        count = sum(allocations.values())
        if count > max(items, raw_items, 20):
            if count != 100:
                allocations = {}
            else:
                allocations = {
                    staff: float(items * Decimal(n) / 100) for staff, n in allocations.items()
                }
        else:
            items = Decimal(count)
    amount = money(order["amount"])
    currency = str(order["currency"]).upper()
    rate = money(context["rates"].get(currency, 1 if currency == "USD" else 0))
    if rate <= 0:
        raise CollectionError("CURRENCY_RATE_MISSING")
    address = entry.get("address") or {}
    customer = " ".join(
        str(address.get(k) or "").strip() for k in ("customerFirstname", "customerLastname")
    ).strip()
    testing = bool(re.search(r"\btest", customer, re.IGNORECASE))
    summary = {
        "orderId": key,
        "clientOrderId": str(order.get("clientOrderId") or ""),
        "customerFullName": customer,
        "recipientPaypal": str(order.get("recipientAccount") or ""),
        "clientSite": str(order.get("clientSite") or ""),
        "category": CATEGORIES[category],
        "sourceCategory": CATEGORIES[category],
        "topInfluencer": influencer,
        "staff": staff,
        "staffAllocations": [{"staff": k, "items": n} for k, n in allocations.items()],
        "amount": float(amount),
        "currency": currency,
        "items": int(items),
        "paymentStatus": str(order["paymentStatus"]),
        "names": names,
        "products": [
            {
                "name": p.get("productName", ""),
                "url": p.get("productUrl", ""),
                "image": p.get("productImg", ""),
                "productId": p.get("productId", ""),
                "quantity": int(n),
            }
            for p, n in zip(products, quantities)
        ],
        "createTime": accounting.strftime("%y-%m-%d %H:%M"),
        "accountingTime": accounting.isoformat(),
        "originalCreateTime": str(order["createTime"]),
        "paymentTime": str(order.get("paymentTime") or order.get("payTime") or ""),
        "updateTime": str(order.get("updateTime") or ""),
        "timeColumnValues": [t.isoformat() for t in times],
        "itemsMethod": entry.get("_collector_items_method", "quantity"),
    }
    # Presence is retained for three-way field merges into ERP.
    columns = {
        "order_time": accounting.isoformat(),
        "source_created_at": accounting.isoformat(),
        "amount_original": str(amount),
        "currency": currency,
        "amount_usd": str((amount / rate).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)),
        "items_count": int(items),
        "product_name": " / ".join(names),
        "order_status": str(order["paymentStatus"]).lower(),
        "classification": category,
        "influencer_name": influencer or None,
        "staff_code": staff or None,
    }
    for field, aliases in {
        "payment_time": ("paymentTime", "payTime", "paidTime"),
        "completed_time": ("completeTime", "completedTime"),
        "source_updated_at": ("updateTime",),
    }.items():
        value = next((order.get(key) for key in aliases if order.get(key)), None)
        if value:
            columns[field] = timestamp(value).isoformat()
    summary["datePolicy"] = "source-create-time-v2"
    summary["legacyAccountingTime"] = max(times).isoformat()
    for source, dest in (
        ("clientOrderId", "client_order_id"),
        ("clientSite", "source_site"),
        ("recipientAccount", "receiving_paypal"),
    ):
        if source in order:
            columns[dest] = order[source] or None
    if "address" in entry:
        columns["customer_name"] = customer or None
    # Do not invent a PayPal transaction id from the DH internal order id.
    if order.get("transactionId"):
        columns["paypal_order_id"] = str(order["transactionId"])
    return {
        "id": key,
        "day": accounting.date().isoformat(),
        "columns": columns,
        "summary": summary,
        "source_updated_at": timestamp(order.get("updateTime")).isoformat()
        if order.get("updateTime")
        else None,
        "testing": testing,
        "context_version": context["version"],
        "fx_rate": str(rate),
    }
