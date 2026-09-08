# 数据库连接与 JSON 编解码：连接按任务创建，统一使用上海时区，不跨事件循环共享连接。
import json
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal

import asyncpg

from app.config.settings import get_settings


def encode(value):
    def default(item):
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError(type(item).__name__)

    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=default)


@asynccontextmanager
async def connect():
    # Connections belong to one task/event loop, never cached across asyncio.run.
    url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(url, command_timeout=120)
    try:
        for kind in ("json", "jsonb"):
            await conn.set_type_codec(kind, encoder=encode, decoder=json.loads, schema="pg_catalog")
        await conn.execute("SET TIME ZONE 'Asia/Shanghai'")
        yield conn
    finally:
        await conn.close()
