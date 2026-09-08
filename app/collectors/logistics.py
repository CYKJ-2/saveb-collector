# 物流来源适配：调用 AfterShip 或快递 100，统一状态和轨迹；不在此层写库。
"""AfterShip 2026-07 and the existing Kuaidi100 stdio queryTrace protocol."""
import re
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from app.config.settings import get_settings
from app.domain.orders import timestamp


class LogisticsError(Exception):
    pass


# auto 优先使用已配置的 AfterShip，其次快递 100；缺少密钥明确报错。
def provider_name(requested="auto"):
    settings = get_settings()
    available = {"aftership": bool(settings.aftership_api_key.get_secret_value().strip()),
                 "kuaidi100": bool(settings.kuaidi100_api_key.get_secret_value().strip())}
    if requested == "auto":
        requested = next((key for key, configured in available.items() if configured), "")
    if not available.get(requested):
        raise LogisticsError("LOGISTICS_NOT_CONFIGURED")
    return requested


def event_time(value):
    if not value:
        return None
    try:
        return timestamp(value).isoformat()
    except Exception:  # noqa: BLE001 - malformed provider date is not the current time
        return None


# 解析旧系统使用的 stdio 文本协议；校验回传单号和有效轨迹，避免空响应被当作成功。
def parse_kuaidi(text, number):
    if re.search(r"额度.*(?:耗尽|不足)|免费.*耗尽", text):
        raise LogisticsError("PROVIDER_QUOTA_EXHAUSTED")
    if re.search(r"参数错误|请求失败|查询失败|无效.*key|key.*错误|unauthorized", text, re.IGNORECASE):
        raise LogisticsError("PROVIDER_REJECTED")
    def field(label):
        match = re.search(r"\*\*" + label + r"\*\*\s*[：:]\s*([^\r\n]+)", text)
        return match.group(1).strip() if match else ""
    returned = field("快递单号")
    if returned and returned != number:
        raise LogisticsError("TRACKING_NUMBER_MISMATCH")
    events = re.findall(r"^\|\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*([^|]+)\|", text, re.MULTILINE)
    if not returned or not events:
        raise LogisticsError("PROVIDER_NO_TRACKING_INFORMATION")
    at, message = max(events, key=lambda event: event[0])
    value = field("物流状态") + " " + message
    status = "unknown"
    for pattern, target in [
        (r"异常|疑难|失败|拒收|退回|退件|破损|丢失|exception|failed|returned", "exception"),
        (r"签收|妥投|已取走|delivered|signed", "delivered"),
        (r"派件|派送|正在投递|out.?for.?delivery", "out_for_delivery"),
        (r"揽收|收寄|运输|在途|转运|到达|离开|发出|投柜|驿站|transit|picked.?up", "in_transit"),
    ]:
        if re.search(pattern, value, re.IGNORECASE):
            status = target
            break
    return {"status": status, "checkpoint": message.strip()[:500], "occurred_at": event_time(at),
            "carrier": field("快递公司"), "provider_id": "", "provider_status": field("物流状态")}


def parse_aftership(payload, number):
    data = payload.get("data", {})
    tracking = data.get("tracking") or data
    if not isinstance(tracking, dict) or tracking.get("tracking_number") != number:
        raise LogisticsError("TRACKING_NUMBER_MISMATCH")
    tag = tracking.get("tag", "Pending")
    status = {"Delivered": "delivered", "InTransit": "in_transit", "OutForDelivery": "out_for_delivery",
              "AttemptFail": "exception", "Exception": "exception", "Expired": "expired",
              "InfoReceived": "pending", "Pending": "pending", "AvailableForPickup": "in_transit"}.get(tag, "unknown")
    checkpoints = tracking.get("checkpoints") or []
    latest = max(checkpoints, key=lambda item: str(item.get("checkpoint_time") or ""), default={})
    return {"status": status, "checkpoint": str(latest.get("message") or "")[:500],
            "occurred_at": event_time(latest.get("checkpoint_time")), "carrier": tracking.get("slug", ""),
            "provider_id": str(tracking.get("id") or ""), "provider_status": str(tag)}


# 只请求固定供应商地址，异常转换为脱敏错误码；带密钥的 URL 不向上层传播。
async def query_tracking(provider, scope, transport=None):
    settings = get_settings()
    number = scope["number"]
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, transport=transport) as client:
            if provider == "kuaidi100":
                reply = await client.get("https://api.kuaidi100.com/stdio/queryTrace", params={
                    "key": settings.kuaidi100_api_key.get_secret_value(), "kuaidiNum": number, "phone": scope.get("phone", ""),
                })
                reply.raise_for_status()
                if len(reply.content) > 1024 * 1024:
                    raise LogisticsError("PROVIDER_RESPONSE_TOO_LARGE")
                result = parse_kuaidi(reply.text, number)
            else:
                base = "https://api.aftership.com/tracking/2026-07"
                headers = {"as-api-key": settings.aftership_api_key.get_secret_value()}
                if scope.get("provider_id"):
                    reply = await client.get(base + "/trackings/" + quote(scope["provider_id"], safe=""), headers=headers)
                else:
                    payload = {"tracking_number": number}
                    aliases = {"顺丰": "sf-express", "中通": "zto-express", "圆通": "yto", "韵达": "yunda", "申通": "sto", "dhl": "dhl", "ups": "ups", "fedex": "fedex"}
                    slug = aliases.get(scope.get("carrier", "").lower())
                    if slug:
                        payload["slug"] = slug
                    reply = await client.post(base + "/trackings", headers=headers, json=payload)
                    if reply.status_code == 400 and reply.json().get("meta", {}).get("code") == 4003:
                        reply = await client.get(base + "/trackings", headers=headers, params={"tracking_numbers": number})
                        reply.raise_for_status()
                        candidates = reply.json().get("data", {}).get("trackings", [])
                        found = next((entry for entry in candidates if entry.get("tracking_number") == number), None)
                        if found is None:
                            raise LogisticsError("PROVIDER_NO_TRACKING_INFORMATION")
                        result = parse_aftership({"data": found}, number)
                        return {**result, "checked_at": datetime.now(UTC).isoformat()}
                reply.raise_for_status()
                result = parse_aftership(reply.json(), number)
            return {**result, "checked_at": datetime.now(UTC).isoformat()}
    except LogisticsError:
        raise
    except httpx.HTTPStatusError as error:
        code = error.response.status_code
        raise LogisticsError("PROVIDER_AUTH_FAILED" if code in (401, 403) else "PROVIDER_QUOTA_EXHAUSTED" if code == 429 else "PROVIDER_HTTP_ERROR") from None
    except Exception:  # noqa: BLE001 - never expose URL with API key / tracking phone
        raise LogisticsError("PROVIDER_QUERY_FAILED") from None
