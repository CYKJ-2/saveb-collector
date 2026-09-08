#!/usr/bin/env python3
# 历史 Pending 手动发现入口，与常规日期刷新分别记录任务和覆盖范围。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.cli import main

if __name__ == "__main__":
    main("pending")
