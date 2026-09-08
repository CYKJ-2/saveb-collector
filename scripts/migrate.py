#!/usr/bin/env python3
# Collector 数据库升级入口：按文件名执行幂等 SQL，并用事务锁防止并发迁移。
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.persistence.db import connect


async def migrate():
    async with connect() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(825723)")
        for path in sorted((Path(__file__).resolve().parents[1] / "migrations").glob("*.sql")):
            await conn.execute(path.read_text(encoding="utf-8-sig"))
    print("collector schema ready; ERP business tables unchanged")


if __name__ == "__main__":
    asyncio.run(migrate())
