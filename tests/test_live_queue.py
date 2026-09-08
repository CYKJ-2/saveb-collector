"""Opt-in real Redis/Celery -> mock HTTP -> actual PostgreSQL contract test."""

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from celery.contrib.testing.worker import start_worker
from conftest import order

from app.config.settings import get_settings
from app.domain.jobs import JobRequest
from app.services.jobs import status, submit


@pytest.mark.asyncio
async def test_real_worker_dispatch_and_database_commit(db, monkeypatch):
    redis_url = os.environ.get("COLLECTOR_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("Set COLLECTOR_TEST_REDIS_URL to a disposable Redis instance")
    if not redis_url.startswith("redis://127.0.0.1:"):
        raise RuntimeError("Test Redis must be localhost")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            assert self.path == "/order/list2"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"code": 1, "totalElement": 1, "data": [order("queue-order")]}).encode()
            )

        def log_message(self, *_):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SAVEB_DH_BASE_URL", f"http://127.0.0.1:{http.server_port}")
    get_settings.cache_clear()
    from app.queue.tasks import celery_app, dispatch

    old_broker, old_backend = celery_app.conf.broker_url, celery_app.conf.result_backend
    celery_app.conf.broker_url = redis_url
    celery_app.conf.result_backend = redis_url
    celery_app.loader.import_default_modules()
    try:
        with start_worker(
            celery_app,
            pool="solo",
            perform_ping_check=False,
            queues=["history", "collect", "maintenance"],
            shutdown_timeout=15,
        ):
            job = await submit(
                db,
                JobRequest(mode="history", start="2026-09-01", end="2026-09-01"),
                "test",
                "real-queue",
            )
            assert await dispatch() == 1
            for _ in range(200):
                result = await status(db, job["jobId"])
                if result["status"] in ("succeeded", "failed", "partial_failed"):
                    break
                await asyncio.sleep(0.1)
            assert result["status"] == "succeeded", result
            assert (
                await db.fetchval("SELECT count(*) FROM orders WHERE order_id='queue-order'") == 1
            )
            assert await db.fetchval("SELECT count(*) FROM collector.outbox") == 0
    finally:
        celery_app.conf.broker_url, celery_app.conf.result_backend = old_broker, old_backend
        http.shutdown()
        http.server_close()
        thread.join(timeout=3)
