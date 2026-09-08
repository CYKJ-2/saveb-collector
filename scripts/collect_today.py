#!/usr/bin/env python3
# 手动当天采集入口：使用北京时间当天范围，通过公共 CLI 提交持久化任务。
"""Collect only the current Asia/Shanghai day using the same job pipeline as the admin button."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.cli import main

if __name__ == "__main__":
    main("today")
