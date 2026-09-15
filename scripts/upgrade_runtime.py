#!/usr/bin/env python3
"""Apply only missing Collector versions after the launcher stops its workers.

The business database must already exist. Never initializes or seeds ERP tables.
The transaction/advisory lock also serializes concurrent deployment attempts.
"""
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.persistence.db import connect


async def upgrade():
    paths = sorted((Path(__file__).resolve().parents[1] / 'migrations').glob('*.sql'))
    applied = []
    async with connect() as conn, conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(825723)')
        current = await conn.fetchval('SELECT max(version) FROM collector.schema_versions')
        latest = max(int(path.name.split('_')[0]) for path in paths)
        if current is None or current > latest:
            raise RuntimeError('Unsupported Collector database version; restore/check the correct database')
        for path in paths:
            version = int(path.name.split('_')[0])
            if version <= current:
                continue
            await conn.execute(path.read_text(encoding='utf-8-sig'))
            actual = await conn.fetchval('SELECT max(version) FROM collector.schema_versions')
            if actual != version:
                raise RuntimeError('Collector migration did not record its version')
            applied.append(version)
    print('Collector versions applied: ' + (','.join(map(str, applied)) or 'none'))


if __name__ == '__main__':
    try:
        asyncio.run(upgrade())
    except Exception as error:
        # Exception messages may contain connection information: do not print them.
        print('Collector upgrade failed: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
