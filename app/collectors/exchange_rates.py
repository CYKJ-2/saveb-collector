# 汇率采集入口：获取并保存汇率快照，供后续任务冻结上下文时读取。
import asyncio
from datetime import date

import httpx

from app.collectors.dh_order import CollectionError
from app.config.settings import get_settings
from app.domain.orders import money
from app.persistence.db import connect
from app.queue.celery_app import celery_app


async def fetch_rates():
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        response = await client.get("https://api.frankfurter.dev/v1/latest?base=USD")
        response.raise_for_status()
        payload = response.json()
    if payload.get("base") != "USD" or not payload.get("rates"):
        raise CollectionError("RATES_SCHEMA_INVALID")
    rates = {**payload["rates"], "USD": 1}
    if any(money(rate) <= 0 for rate in rates.values()):
        raise CollectionError("RATES_VALUE_INVALID")
    async with connect() as conn:  # noqa: SIM117 - explicit transaction lifetime
        async with conn.transaction():
            effective_date = date.fromisoformat(payload["date"])
            await conn.execute(
                "INSERT INTO collector.exchange_rates(effective_date,rates,source) VALUES($1,$2,$3) ON CONFLICT(effective_date) DO NOTHING",
                effective_date,
                rates,
                "Frankfurter/currency-per-USD",
            )
            if get_settings().publish_api:
                # API 的 rate_to_usd 是乘数，与来源 rates 的除数互为倒数。
                await conn.executemany(
                    "INSERT INTO exchange_rates(currency,rate_to_usd,effective_date) VALUES($1,$2,$3) ON CONFLICT(currency,effective_date) DO NOTHING",
                    [
                        (currency, money(1) / money(rate), effective_date)
                        for currency, rate in rates.items()
                    ],
                )
    return {"effectiveDate": payload["date"], "currencies": len(rates)}


@celery_app.task(
    name="collector.exchange_rates",
    autoretry_for=(httpx.HTTPError,),
    retry_backoff=True,
    max_retries=3,
)
def fetch_rates_task():
    return asyncio.run(fetch_rates())
