#!/usr/bin/env python3
# 指定日期范围对账入口：使用一致性事务读取，生成报告而不改写订单。
import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.persistence.db import connect, encode
from app.services.reconciliation import reconcile


async def run(args):
    async with connect() as conn, conn.transaction(isolation="repeatable_read"):
        report = await reconcile(conn, args.start, args.end)
    content = encode(report)
    if args.output:
        Path(args.output).write_text(content + "\n", encoding="utf-8")
    print(content)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only business reconciliation; persist audit report")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("start must be <= end")
    raise SystemExit(asyncio.run(run(args)))
