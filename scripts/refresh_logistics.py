#!/usr/bin/env python3
# 物流手动入口：提交一批或指定采购任务的查询，实际 HTTP 请求由 logistics worker 执行。
import argparse
import asyncio
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.collectors.logistics import LogisticsError
from app.persistence.db import connect, encode
from app.services.logistics import LogisticsRequest, submit


async def run(args):
    async with connect() as conn:
        print(encode(await submit(conn, LogisticsRequest(provider=args.provider, taskId=args.task_id), "operator-cli", str(uuid4()))))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Submit a durable logistics refresh job")
    parser.add_argument("--task-id", type=int)
    parser.add_argument("--provider", choices=["auto", "aftership", "kuaidi100"], default="auto")
    try:
        asyncio.run(run(parser.parse_args()))
    except (LogisticsError, ValueError) as error:
        parser.error(str(error))
