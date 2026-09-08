from urllib.parse import parse_qs

import httpx
import pytest

from app.collectors.errors import CollectionError
from app.collectors.session import login
from app.config.settings import Settings


def settings(**kwargs):
    return Settings(
        _env_file=None, dh_username="example-user", dh_password="example-secret", **kwargs
    )


@pytest.mark.asyncio
async def test_login_form_and_protected_probe_use_the_same_cookie_jar():
    def handler(request):
        if request.url.path == "/login" and request.method == "GET":
            return httpx.Response(
                200, text="login", headers={"set-cookie": "JSESSIONID=challenge; Path=/; Secure"}
            )
        assert "JSESSIONID=challenge" in request.headers["cookie"]
        if request.url.path == "/captcha":
            return httpx.Response(200, content=b"captcha")
        if request.url.path == "/login":
            form = parse_qs(request.content.decode())
            assert form["username"] == ["example-user"]
            assert form["password"] == ["example-secret"]
            assert form["captcha"] == ["abcd"]
            return httpx.Response(302, headers={"location": "/order/index2"})
        assert request.url.path == "/order/list2"
        assert request.headers["content-type"] == "application/json"
        return httpx.Response(200, json={"code": 1, "data": [], "totalElement": 0})

    cookie = await login(settings(), httpx.MockTransport(handler), solver=lambda _: "abcd")
    assert cookie == "JSESSIONID=challenge"


@pytest.mark.asyncio
async def test_external_redirect_is_never_followed():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(302, headers={"location": "https://another.invalid/login"})
        return httpx.Response(200, content=b"image")

    with pytest.raises(CollectionError, match="AUTH_UNEXPECTED_REDIRECT"):
        await login(settings(), httpx.MockTransport(handler), solver=lambda _: "abcd")


@pytest.mark.asyncio
async def test_rejected_password_stops_without_retry_or_secret_in_error():
    submitted = []

    def handler(request):
        if request.url.path == "/order/list2":
            return httpx.Response(302, headers={"location": "/login"})
        if request.method == "POST":
            submitted.append(1)
            return httpx.Response(200, text="Bad credentials")
        return httpx.Response(200, content=b"image")

    with pytest.raises(CollectionError, match="AUTH_CREDENTIALS_REJECTED") as error:
        await login(settings(), httpx.MockTransport(handler), solver=lambda _: "abcd")
    assert len(submitted) == 1
    assert "example-secret" not in str(error.value)


@pytest.mark.asyncio
async def test_bad_captcha_is_bounded():
    attempts = []

    def unreadable(_):
        attempts.append(1)
        raise CollectionError("AUTH_CAPTCHA_UNREADABLE")

    with pytest.raises(CollectionError, match="AUTH_LOGIN_REJECTED"):
        await login(
            settings(dh_login_attempts=2),
            httpx.MockTransport(lambda _: httpx.Response(200)),
            solver=unreadable,
        )
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_expired_collection_reauthenticates_once(monkeypatch):
    from app.collectors import dh_order

    monkeypatch.setenv("SAVEB_DH_USERNAME", "example-user")
    monkeypatch.setenv("SAVEB_DH_PASSWORD", "example-secret")
    seen = []

    async def cookie(rejected=None):
        seen.append(rejected)
        return "session=new" if rejected else "session=old"

    monkeypatch.setattr(dh_order, "session_cookie", cookie)

    def handler(request):
        if request.headers["cookie"] == "session=old":
            return httpx.Response(302, headers={"location": "/login"})
        return httpx.Response(200, json={"code": 1, "data": [], "totalElement": 0})

    async with dh_order.DHOrderClient(httpx.MockTransport(handler)) as client:
        assert await client.collect({"day": "2026-09-07"}) == []
    assert seen == [None, "session=old"]
