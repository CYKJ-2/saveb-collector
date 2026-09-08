# 登录与共享会话：识别验证码、验证登录结果，通过 Redis 锁避免并发重复登录。
"""Portable account login with bounded CAPTCHA handling and shared session caching."""

import asyncio
import hashlib
import re
import time
from datetime import datetime
from functools import lru_cache
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.collectors.errors import CollectionError
from app.config.settings import get_settings

USER_AGENT = "Mozilla/5.0 (compatible; SavebCollector/1.1)"


@lru_cache(maxsize=1)
def captcha_model():
    try:
        import ddddocr

        return ddddocr.DdddOcr(show_ad=False)
    except Exception:  # noqa: BLE001 - redact third-party library details
        raise CollectionError("AUTH_CAPTCHA_ENGINE_UNAVAILABLE") from None


def recognize(image):
    try:
        answer = captcha_model().classification(image)
        answer = re.sub(r"[^a-zA-Z0-9]", "", answer).lower()
        if len(answer) != 4:
            raise CollectionError("AUTH_CAPTCHA_UNREADABLE")
        return answer
    except CollectionError:
        raise
    except Exception:  # noqa: BLE001 - redact third-party library details
        raise CollectionError("AUTH_CAPTCHA_UNREADABLE") from None


def configured(settings):
    return bool(settings.dh_username.get_secret_value() and settings.dh_password.get_secret_value())


# 验证码通过不等于登录成功，还需验证来源接口返回结构后才提取会话。
async def login(settings, transport=None, solver=recognize):
    """Normal form authentication. Redirects are inspected, never followed across hosts."""
    if not configured(settings):
        raise CollectionError("AUTH_CREDENTIALS_MISSING")
    origin = settings.dh_base_url.rstrip("/")
    for attempt in range(settings.dh_login_attempts):
        async with httpx.AsyncClient(
            base_url=origin,
            timeout=20,
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            page = await client.get("/login")
            if page.status_code != 200:
                raise CollectionError("AUTH_LOGIN_PAGE_UNAVAILABLE")
            challenge = await client.get("/captcha", params={"renew": str(time.time_ns())})
            if challenge.status_code != 200 or len(challenge.content) > 1024 * 1024:
                raise CollectionError("AUTH_CAPTCHA_UNAVAILABLE")
            try:
                answer = await asyncio.to_thread(solver, challenge.content)
            except CollectionError as exc:
                if str(exc) != "AUTH_CAPTCHA_UNREADABLE":
                    raise
                continue
            response = await client.post(
                "/login",
                data={
                    "username": settings.dh_username.get_secret_value(),
                    "password": settings.dh_password.get_secret_value(),
                    "captcha": answer,
                    "remember-me": "on",
                },
                headers={"Origin": origin, "Referer": origin + "/login"},
            )
            location = response.headers.get("location", "")
            if location and urlsplit(urljoin(origin, location)).netloc != urlsplit(origin).netloc:
                raise CollectionError("AUTH_UNEXPECTED_REDIRECT")
            # Verify access using the actual protected JSON endpoint; a 302 alone is not success.
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            probe = await client.post(
                "/order/list2",
                json={
                    "pageNum": 1,
                    "pageSize": 1,
                    "orderTest": "live",
                    "clientSite": "",
                    "centerSite": "",
                    "recipientAccount": "",
                    "paymentStatus": "",
                    "duration": "",
                    "orderId": "",
                    "clientOrderId": "",
                    "transactionId": "",
                    "startDate": today + " ",
                    "endDate": " " + today,
                },
                headers={"Origin": origin, "Referer": origin + "/order/index2"},
            )
            try:
                payload = probe.json()
            except ValueError:
                payload = {}
            if (
                probe.status_code == 200
                and isinstance(payload, dict)
                and str(payload.get("code")) == "1"
                and (
                    isinstance(payload.get("data"), list)
                    or (
                        "data" in payload
                        and "totalElement" in payload
                        and payload.get("data") is None
                        and payload.get("totalElement") is None
                        and payload.get("msg") == "Success"
                    )
                )
            ):
                host = urlsplit(origin).hostname
                cookie = "; ".join(
                    f"{c.name}={c.value}"
                    for c in client.cookies.jar
                    if (host == c.domain.lstrip(".") or host.endswith("." + c.domain.lstrip(".")))
                    and not c.is_expired()
                )
                if cookie and not any(char in cookie for char in "\r\n"):
                    return cookie
                raise CollectionError("AUTH_SESSION_COOKIE_MISSING")
            # Read only same-origin login feedback to distinguish bad credentials from CAPTCHA.
            if location and urlsplit(urljoin(origin, location)).path == "/login":
                response = await client.get(location)
            message = response.content.decode("utf-8", errors="replace")[:50000]
            if re.search(
                r"密码错误|用户名或密码|账号或密码|账户或密码|invalid.{0,20}(user|password)|Bad credentials",
                message,
                re.IGNORECASE,
            ):
                raise CollectionError("AUTH_CREDENTIALS_REJECTED")
            if attempt + 1 < settings.dh_login_attempts:
                await asyncio.sleep(1)
    raise CollectionError("AUTH_LOGIN_REJECTED")


# 优先复用其他 worker 已更新的共享会话；登录失败设置冷却，避免每个排队任务重复撞密码。
async def session_cookie(rejected=None):
    settings = get_settings()
    if not configured(settings):
        cookie = settings.dh_cookie.get_secret_value()
        if not cookie:
            raise CollectionError("AUTH_COOKIE_MISSING")
        if rejected is not None:
            raise CollectionError("AUTH_SESSION_INVALID")
        return cookie
    identity = (
        settings.dh_base_url
        + ":"
        + settings.source_account
        + ":"
        + settings.dh_username.get_secret_value()
    )
    key = "collector:session:" + hashlib.sha256(identity.encode()).hexdigest()
    redis = Redis.from_url(
        settings.celery_broker_url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    try:
        async with redis.lock(key + ":lock", timeout=120, blocking_timeout=10):
            cached = await redis.get(key)
            if cached and cached != rejected:
                return cached
            if await redis.get(key + ":cooldown"):
                raise CollectionError("AUTH_LOGIN_COOLDOWN")
            try:
                async with asyncio.timeout(90):
                    cookie = await login(settings)
            except Exception as exc:  # noqa: BLE001 - never expose credentials
                # Prevent every queued job from retrying a rejected password.
                code = str(exc) if isinstance(exc, CollectionError) else "AUTH_LOGIN_UNAVAILABLE"
                await redis.set(key + ":cooldown", code, ex=300)
                raise CollectionError(code) from None
            await redis.set(key, cookie, ex=settings.dh_session_ttl)
            return cookie
    except RedisError:
        raise CollectionError("AUTH_SESSION_STORE_UNAVAILABLE") from None
    finally:
        await redis.aclose()
