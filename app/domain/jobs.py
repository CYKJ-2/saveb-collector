# 采集请求模型：定义任务模式、日期范围和预览参数，提交前统一校验。
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    mode: Literal["refresh", "today", "missing", "history", "reprocess", "pending"] = "refresh"
    start: date | None = None
    end: date | None = None
    # 2 表示包含今天和昨天，不是额外回看两天；显式日期范围优先于该默认值。
    lookback_days: int = Field(2, ge=1, le=31, alias="lookbackDays")
    include_open_orders: bool = Field(True, alias="includeOpenOrders")
    dry_run: bool = Field(False, alias="dryRun")
    source_job_id: str | None = Field(None, alias="sourceJobId")

    @model_validator(mode="after")
    def validate_range(self):
        if self.mode not in ("refresh", "today") and (not self.start or not self.end):
            raise ValueError("start and end are required")
        if bool(self.start) != bool(self.end) or (self.start and self.start > self.end):
            raise ValueError("invalid date range")
        if self.start and self.start < date(2000, 1, 1):
            raise ValueError("start must be >= 2000-01-01")
        if self.mode == "reprocess" and not self.source_job_id:
            raise ValueError("sourceJobId is required for reprocess")
        if self.mode == "reprocess":
            self.dry_run = True
        if self.mode == "today" and (self.start or self.end):
            raise ValueError("today uses the server's Asia/Shanghai date; omit dates")
        return self
