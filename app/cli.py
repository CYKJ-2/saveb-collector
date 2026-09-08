# 命令行公共入口：把参数转换为持久化采集任务；--wait 只等待结果，超时不取消任务。
import argparse
import asyncio
import time
from uuid import uuid4

from app.domain.jobs import JobRequest
from app.persistence.db import connect, encode
from app.services.jobs import TERMINAL, resume, status, submit


# CLI 直接调用持久化任务服务；等待超时返回单独退出码，后台任务仍继续运行。
async def run(args, default_mode):
    async with connect() as conn:
        if args.resume:
            job = await resume(conn, args.resume)
        else:
            mode = (
                ("history" if args.mode == "refresh" else args.mode)
                if default_mode == "history"
                else default_mode
            )
            request = JobRequest(
                mode=mode,
                start=args.start,
                end=args.end,
                lookback_days=args.lookback_days,
                include_open_orders=not args.no_open_orders,
                dry_run=args.dry_run,
                source_job_id=args.source_job_id,
            )
            job = await submit(conn, request, args.actor, args.idempotency_key or str(uuid4()))
        print(encode({"jobId": job["jobId"], "status": job["status"]}), flush=True)
    if not args.wait:
        return 0
    deadline = time.monotonic() + args.wait_timeout
    while time.monotonic() < deadline:
        async with connect() as conn:
            job = await status(conn, job["jobId"])
        if job["status"] in TERMINAL:
            print(encode(job), flush=True)
            return 0 if job["status"] == "succeeded" else 1
        await asyncio.sleep(2)
    print(encode({"jobId": job["jobId"], "error": "WAIT_TIMEOUT_JOB_CONTINUES"}))
    return 2


def main(default_mode):
    parser = argparse.ArgumentParser()
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--mode", choices=["missing", "refresh", "reprocess"], default="refresh")
    parser.add_argument("--lookback-days", type=int, default=2)
    parser.add_argument("--include-open-orders", action="store_true", help="Enabled by default")
    parser.add_argument("--no-open-orders", action="store_true")
    parser.add_argument("--chunk-days", type=int, choices=[1], default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source-job-id")
    parser.add_argument("--resume")
    parser.add_argument(
        "--failed-only", action="store_true", help="Resume always retries failed chunks only"
    )
    parser.add_argument("--idempotency-key")
    parser.add_argument("--actor", default="operator-cli")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--wait-timeout", type=int, default=3600)
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run(args, default_mode)))
    except (ValueError, LookupError) as exc:
        parser.error(str(exc))
