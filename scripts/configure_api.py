#!/usr/bin/env python3
# 本地接入配置工具：读取 saveb-api 数据库配置并同步服务令牌，不自动启动采集。
"""Configure the local Docker integration using saveb-api's existing DB credentials.

Does not print credentials, fetch orders, or start services. Existing DH Cookie is preserved.
"""

import argparse
import secrets
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values


def update(path, values):
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    for key, value in values.items():
        # dotenv double quoting also supports credentials containing spaces or #.
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        line = f'{key}="{escaped}"'
        indices = [i for i, old in enumerate(lines) if old.strip().startswith(key + "=")]
        if indices:
            for i in indices:
                lines[i] = line
        else:
            lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-dir", type=Path, default=Path(__file__).resolve().parents[2] / "saveb-api"
    )
    args = parser.parse_args()
    api_env = args.api_dir / ".env"
    env = dotenv_values(api_env)
    if not all(env.get(k) for k in ("DB_DATABASE", "DB_USERNAME", "DB_PASSWORD")):
        raise SystemExit("saveb-api DB configuration is incomplete")
    target = Path(__file__).resolve().parents[1] / ".env"
    current = dotenv_values(target)
    token = (
        env.get("SAVEB_COLLECTOR_TOKEN")
        or current.get("SAVEB_API_TOKEN")
        or secrets.token_urlsafe(48)
    )
    # Shared network's unique container name avoids resolving the collector's Redis/PG by mistake.
    database_url = "postgresql://{}:{}@saveb-api-postgres:5432/{}".format(
        quote(env["DB_USERNAME"], safe=""),
        quote(env["DB_PASSWORD"], safe=""),
        quote(env["DB_DATABASE"], safe=""),
    )
    update(
        target,
        {
            "SAVEB_DATABASE_URL": database_url,
            "SAVEB_API_TOKEN": token,
            "SAVEB_PUBLISH_API": "true",
            "SAVEB_DH_BASE_URL": "https://www.dh-order.com",
            "SAVEB_CELERY_BROKER_URL": "redis://saveb-collector-redis:6379/1",
            "SAVEB_CELERY_RESULT_BACKEND": "redis://saveb-collector-redis:6379/2",
            "SAVEB_SOURCE_ACCOUNT": env.get("SAVEB_COLLECTOR_ACCOUNT") or "default",
        },
    )
    update(
        api_env,
        {
            "SAVEB_COLLECTOR_URL": "http://saveb-collector-api:8080",
            "SAVEB_COLLECTOR_TOKEN": token,
            "SAVEB_COLLECTOR_ACCOUNT": env.get("SAVEB_COLLECTOR_ACCOUNT") or "default",
        },
    )
    print("Configured saveb-api database and matching service token; no business data changed.")
    if not current.get("SAVEB_DH_COOKIE"):
        print("SAVEB_DH_COOKIE still required before starting collection workers.")


if __name__ == "__main__":
    main()
