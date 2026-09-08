# 构建任务规则快照：合并站点、达人和汇率配置，生成版本摘要以便复现计算。
import json
from pathlib import Path

from app.collectors.dh_order import CollectionError
from app.config.settings import get_settings
from app.domain.orders import digest, domain


# 规则和汇率读取后计算版本摘要并随任务保存，重试时无需重新解释当前配置。
async def load_context(conn):
    settings = get_settings()
    if settings.publish_api:
        for table in ("orders", "api_tokens", "permissions"):
            if not await conn.fetchval("SELECT to_regclass($1)", "public." + table):
                raise CollectionError("API_DATABASE_SCHEMA_REQUIRED")
    context = {}
    if await conn.fetchval("SELECT to_regclass('public.system_state')"):
        context = (
            await conn.fetchval(
                "SELECT value FROM system_state WHERE key='legacy_dashboard_context' AND deleted_at IS NULL"
            )
            or {}
        )
    rules = context.get("siteRules", {})
    rates = context.get("exchangeRates", {})
    latest_rates = await conn.fetchval(
        "SELECT rates FROM collector.exchange_rates ORDER BY effective_date DESC LIMIT 1"
    )
    if latest_rates:
        rates = latest_rates
    if settings.rules_file:
        rules = json.loads(Path(settings.rules_file).read_text(encoding="utf-8-sig"))
    if settings.rates_file:
        rates = json.loads(Path(settings.rates_file).read_text(encoding="utf-8-sig"))
    rates = rates.get("rates", rates)
    if not rules or not isinstance(rates, dict):
        raise CollectionError("RULES_CONTEXT_MISSING")
    rules = json.loads(json.dumps(rules))
    for key in ("domainMap", "topInfluencerMap", "authority"):
        rules[key] = {domain(k): v for k, v in rules.get(key, {}).items()}
    # Authority mapping is versioned in this package and matches ERP's source authority.
    authority = json.loads(
        (Path(__file__).parents[1] / "domain" / "site_authority.json").read_text(
            encoding="utf-8-sig"
        )
    )
    rules["authority"].update(authority)
    if await conn.fetchval("SELECT to_regclass('public.influencer_domains')"):
        for row in await conn.fetch(
            "SELECT domain, influencer_name FROM influencer_domains WHERE confirmed=true AND influencer_name IS NOT NULL AND deleted_at IS NULL"
        ):
            rules["topInfluencerMap"][domain(row["domain"])] = row["influencer_name"]
    rates = {**rates, "USD": 1}
    value = {
        "rules": rules,
        "rates": rates,
        "date_policy": "source-create-time-v2",
        "items_policy": "legacy-images-quantity-fallback-v1",
    }
    return {**value, "version": digest(value)}
