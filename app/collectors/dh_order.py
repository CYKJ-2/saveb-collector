# DH 来源适配：登录会话、分页请求与完整性校验；返回原始订单供服务层处理。
"""Legacy protocol: complete Cookie header, POST /order/list2 and verified pagination."""

import asyncio
from datetime import date, timedelta

import httpx

from app.collectors.errors import CollectionError
from app.collectors.session import configured, session_cookie
from app.config.settings import get_settings


class DHOrderClient:
    def __init__(self, transport=None):
        self.settings = get_settings()
        self.http = httpx.AsyncClient(
            base_url=self.settings.dh_base_url,
            timeout=self.settings.fetch_timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.http.aclose()

    async def page(self, params):
        cookie = await session_cookie()
        try:
            return await self._page(params, cookie)
        except CollectionError as error:
            if str(error) not in ("AUTH_SESSION_INVALID", "UPSTREAM_NOT_JSON") or not configured(
                self.settings
            ):
                raise
            cookie = await session_cookie(rejected=cookie)
            return await self._page(params, cookie)

    async def _page(self, params, cookie):
        for attempt in range(3):
            try:
                await asyncio.sleep(self.settings.request_interval_seconds)
                response = await self.http.post(
                    "/order/list2",
                    json=params,
                    headers={
                        "Cookie": cookie,
                        "Origin": self.settings.dh_base_url,
                        "Referer": self.settings.dh_base_url + "/order/index2",
                        "User-Agent": "saveb-collector/1.0",
                        "Accept": "application/json",
                    },
                )
                if response.status_code in (301, 302, 303, 307, 308, 401, 403):
                    raise CollectionError("AUTH_SESSION_INVALID")
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.TransportError("upstream unavailable")
                if response.status_code != 200:
                    raise CollectionError("UPSTREAM_HTTP_ERROR")
                try:
                    value = response.json()
                except ValueError:
                    raise CollectionError("UPSTREAM_NOT_JSON") from None
                # DH-Order's verified empty-date response contains explicit nulls, not [].
                if (
                    isinstance(value, dict)
                    and str(value.get("code")) == "1"
                    and value.get("msg") == "Success"
                    and "data" in value
                    and "totalElement" in value
                    and value["data"] is None
                    and value["totalElement"] is None
                ):
                    return 0, []
                if not isinstance(value, dict) or not isinstance(value.get("data"), list):
                    raise CollectionError("UPSTREAM_SCHEMA_INVALID")
                if str(value.get("code")) != "1":
                    raise CollectionError("UPSTREAM_REJECTED_REQUEST")
                total = value.get("totalElement")
                if (
                    isinstance(total, bool)
                    or not isinstance(total, (int, str))
                    or not str(total).isdigit()
                ):
                    raise CollectionError("UPSTREAM_TOTAL_MISSING")
                return int(total), value["data"]
            except httpx.HTTPError:
                if attempt == 2:
                    raise CollectionError("UPSTREAM_RETRY_EXHAUSTED") from None
                await asyncio.sleep(2**attempt)

    # scope 支持日期、历史 Pending 区间和单号；全部分页验证通过后才返回，禁止发布半份结果。
    async def collect(self, scope):
        params = {
            "pageSize": 100,
            "pageNum": 1,
            "clientSite": "",
            "centerSite": "",
            "recipientAccount": "",
            "paymentStatus": "",
            "duration": "",
            "orderId": "",
            "clientOrderId": "",
            "transactionId": "",
            "orderTest": "live",
        }
        if "pending_start" in scope:
            params.update(
                paymentStatus="Pending",
                startDate=f"{date.fromisoformat(scope['pending_start']) - timedelta(days=1)} ",
                endDate=f" {date.fromisoformat(scope['pending_end']) + timedelta(days=1)}",
            )
        elif "day" in scope:
            day = date.fromisoformat(scope["day"])
            params.update(
                startDate=f"{day - timedelta(days=1)} ", endDate=f" {day + timedelta(days=1)}"
            )
        else:
            params["orderId"] = scope["order_id"]
        total, rows = await self.page(params)
        pages = max(1, (total + 99) // 100)
        budget = self.settings.pending_max_pages if "pending_start" in scope else self.settings.max_pages
        if pages > budget:
            raise CollectionError("PAGE_BUDGET_EXCEEDED")
        for page in range(2, pages + 1):
            current_total, batch = await self.page({**params, "pageNum": page})
            if total != current_total:
                raise CollectionError("PAGINATION_CHANGED_RETRY_CHUNK")
            rows.extend(batch)
        if len(rows) != total:
            raise CollectionError("PAGINATION_INCOMPLETE")
        ids = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("order"), dict):
                raise CollectionError("ORDER_SCHEMA_INVALID")
            key = str(row["order"].get("orderId") or "").strip()
            if not key:
                raise CollectionError("ORDER_ID_MISSING")
            ids.append(key)
        if len(set(ids)) != len(ids):
            raise CollectionError("DUPLICATE_ORDER_ID")
        if "order_id" in scope and (len(ids) != 1 or ids[0] != scope["order_id"]):
            raise CollectionError("ORDER_LOOKUP_INCOMPLETE")
        return rows
