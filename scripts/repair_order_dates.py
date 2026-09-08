#!/usr/bin/env python3
# 服务器迁移后的日期修正入口：分批处理并汇总结果；是否实际写入由命令参数决定。
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.persistence.db import connect, encode
from app.services.order_dates import repair_batch


async def run(args):
    after = 0
    totals = {}
    async with connect() as conn:
        while True:
            report = await repair_batch(conn, after, args.apply)
            if not report["scanned"]:
                break
            after = report.pop("after_id")
            for key, value in report.items():
                totals[key] = totals.get(key, 0) + value
    print(encode({"apply": args.apply, **totals}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preview legacy date repair; --apply writes proven dates and rebuilds affected statistics")
    parser.add_argument("--apply", action="store_true")
    asyncio.run(run(parser.parse_args()))
