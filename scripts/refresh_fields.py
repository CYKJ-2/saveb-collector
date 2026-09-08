#!/usr/bin/env python3
# 常规刷新入口：默认当天＋前一天，结合未完成订单复查与滚动历史日补采。
"""Submit the same refresh job as Beat/manual API."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.cli import main

if __name__ == "__main__":
    main("refresh")
