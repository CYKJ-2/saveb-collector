#!/usr/bin/env python3
# 兼容导出入口：将已有兼容快照写到指定目录，供旧格式消费方使用。
"""Optional immutable compatibility export; no business database mutations."""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.domain.orders import digest
from app.persistence.db import connect, encode


async def export(output):
    output = Path(output).resolve()
    run = output / ("export-" + str(uuid4()))
    run.mkdir(parents=True)
    manifest = {"version": 1, "files": {}}
    async with connect() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        for row in await conn.fetch("SELECT day,payload FROM legacy_dashboard_days ORDER BY day"):
            name = f"history/{row['day']}.json"
            target = run / name
            target.parent.mkdir(exist_ok=True)
            target.write_text(encode(row["payload"]), encoding="utf-8")
            manifest["files"][name] = digest(row["payload"])
        latest = await conn.fetchval(
            "SELECT value FROM system_state WHERE key='legacy_dashboard_latest'"
        )
        if latest:
            (run / "latest.json").write_text(encode(latest["payload"]), encoding="utf-8")
            manifest["files"]["latest.json"] = digest(latest["payload"])
    (run / "manifest.json").write_text(encode(manifest), encoding="utf-8")
    temp = output / ("current-" + str(uuid4()) + ".tmp")
    temp.write_text(encode({"directory": run.name}), encoding="utf-8")
    os.replace(temp, output / "current.json")
    print(str(run))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    asyncio.run(export(parser.parse_args().output))
