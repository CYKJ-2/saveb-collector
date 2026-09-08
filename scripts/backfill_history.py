#!/usr/bin/env python3
# 历史范围入口：支持范围更新、仅补未成功日期和失败分片续跑；参数交由公共 CLI 校验。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.cli import main

if __name__ == "__main__":
    main("history")
