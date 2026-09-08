#!/usr/bin/env python3
# 启动前检查：验证依赖连接及规则上下文，尽早发现配置缺失。
"""Read-only checks. Never calls the upstream or publishes business data."""

import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config.settings import get_settings
from app.persistence.db import connect, encode
from app.services.context import load_context


async def check():
    settings = get_settings()
    errors = []
    if urlsplit(settings.dh_base_url).hostname != "www.dh-order.com":
        errors.append("SAVEB_DH_BASE_URL must target the verified www.dh-order.com service")
    if not settings.dh_cookie.get_secret_value() and not (
        settings.dh_username.get_secret_value() and settings.dh_password.get_secret_value()
    ):
        errors.append("Set SAVEB_DH_USERNAME + SAVEB_DH_PASSWORD, or SAVEB_DH_COOKIE")
    if len(settings.api_token.get_secret_value()) < 32:
        errors.append("SAVEB_API_TOKEN should contain at least 32 random characters")
    async with connect() as conn:
        await conn.fetchval("SELECT max(version) FROM collector.schema_versions")
        context = await load_context(conn)
        if settings.publish_api:
            for table in (
                "orders",
                "daily_stats",
                "system_state",
                "legacy_dashboard_days",
                "api_tokens",
                "permissions",
            ):
                if not await conn.fetchval("SELECT to_regclass($1)", "public." + table):
                    errors.append("Missing saveb-api table: " + table)
            owner = await conn.fetchval("SELECT current_database()")
        else:
            owner = await conn.fetchval("SELECT current_database()")
    from redis.asyncio import Redis

    client = Redis.from_url(settings.celery_broker_url, socket_connect_timeout=5)
    try:
        await client.ping()
    finally:
        await client.aclose()
    print(
        encode(
            {
                "ok": not errors,
                "database": owner,
                "publication": "saveb-api" if settings.publish_api else "shadow",
                "rulesVersion": context["version"],
                "errors": errors,
            }
        )
    )
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(check()))
    except Exception as exc:  # noqa: BLE001 - do not print protected DSNs
        print(
            encode({"ok": False, "error": "PREFLIGHT_DEPENDENCY_ERROR", "type": type(exc).__name__})
        )
        raise SystemExit(1) from None
