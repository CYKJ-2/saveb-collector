#!/usr/bin/env python3
# 登录诊断入口：验证 DH 会话是否可用，不输出账号密码或完整 Cookie。
"""Verify configured account and cache session; never print its cookie or credentials."""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.collectors.errors import CollectionError
from app.collectors.session import session_cookie


async def main():
    await session_cookie()
    print(json.dumps({"ok": True, "session": "ready"}))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except CollectionError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        raise SystemExit(1) from None
