# 集中读取 SAVEB_ 环境变量及 .env，校验来源地址；密码和物流密钥使用 SecretStr。
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SAVEB_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )
    database_url: str = "postgresql://saveb:saveb@localhost:5432/saveb"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    redis_url: str = "redis://localhost:6379/0"
    api_token: SecretStr = SecretStr("")
    dh_cookie: SecretStr = SecretStr("")
    dh_username: SecretStr = SecretStr("")
    dh_password: SecretStr = SecretStr("")
    dh_login_attempts: int = Field(3, ge=1, le=5)
    dh_session_ttl: int = Field(28800, ge=60, le=604800)
    dh_base_url: str = "https://www.dh-order.com"
    source_account: str = "default"
    fetch_timeout_seconds: int = Field(60, ge=1, le=240)
    max_pages: int = Field(200, ge=1, le=1000)
    request_interval_seconds: float = Field(0.5, ge=0)
    max_attempts: int = Field(3, ge=1, le=10)
    history_start: str = "2021-08-10"
    pending_enabled: bool = True
    # 自动 Pending 发现只循环最近 N 个自然日（含今天），不影响手动范围采集。
    pending_lookback_days: int = Field(30, ge=1, le=366)
    pending_interval_minutes: int = Field(30, ge=5, le=1440)
    pending_window_days: int = Field(7, ge=1, le=31)
    pending_max_pages: int = Field(10, ge=1, le=200)
    pending_timeout_seconds: int = Field(240, ge=10, le=600)
    # 必须显式开启业务发布；否则只保存来源层，不更新 saveb-api 的订单表。
    publish_api: bool = False
    rules_file: str = ""
    rates_file: str = ""
    app_env: str = "local"
    # 物流密钥仅留在 Collector；当前可保持空值，配置后再启用真实供应商查询。
    aftership_api_key: SecretStr = SecretStr("")
    kuaidi100_api_key: SecretStr = SecretStr("")
    logistics_enabled: bool = True
    logistics_interval_minutes: int = Field(30, ge=5, le=1440)
    logistics_batch_size: int = Field(20, ge=1, le=100)

    @model_validator(mode="after")
    def verified_upstream(self):
        target = urlsplit(self.dh_base_url)
        if self.dh_base_url.rstrip("/") == "https://www.dh-order.com":
            return self
        if (
            self.app_env == "test"
            and target.scheme == "http"
            and target.hostname in ("127.0.0.1", "localhost")
            and not target.username
            and not target.password
        ):
            return self
        raise ValueError(
            "SAVEB_DH_BASE_URL must be https://www.dh-order.com; legacy dhgate configuration is not supported"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
