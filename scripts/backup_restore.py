#!/usr/bin/env python3
# 备份运维入口：全库快照与附件校验后，在随机临时库真实恢复验证；不覆盖业务库。
"""Whole database + attachment backup; restore ONLY to an internally generated scratch DB.

Works with native PostgreSQL clients or docker exec into the PostgreSQL container.
Credentials are supplied through environment variables, never command arguments/log output.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class BackupError(RuntimeError):
    pass


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def quoted(value):
    return '"' + value.replace('"', '""') + '"'


def archive_hashes(path):
    result = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            if member.name.startswith("/") or ".." in Path(member.name).parts:
                raise BackupError("Unsafe attachment archive member")
            if member.isdir():
                continue
            if not member.isfile() or member.name in result:
                raise BackupError("Unsupported or duplicate attachment archive member")
            stream = archive.extractfile(member)
            value = hashlib.sha256()
            for block in iter(lambda stream=stream: stream.read(1024 * 1024), b""):
                value.update(block)
            result[member.name] = value.hexdigest()
    return result


class Postgres:
    def __init__(self, url, container=None):
        self.url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
        self.container = container

    def command(self, program, args, database=None):
        env = dict(os.environ)
        target = urlsplit(self.url)
        env.update(PGDATABASE=database or unquote(target.path.lstrip("/")),
                   PGHOST=target.hostname or "localhost", PGPORT=str(target.port or 5432),
                   PGUSER=unquote(target.username or ""), PGPASSWORD=unquote(target.password or ""),
                   PGCONNECT_TIMEOUT="15")
        query = parse_qs(target.query)
        if "sslmode" in query:
            env["PGSSLMODE"] = query["sslmode"][0]
        if self.container:
            prefix = ["docker", "exec", "-i"]
            for name in ("PGDATABASE", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGCONNECT_TIMEOUT", "PGSSLMODE"):
                if name in env:
                    prefix += ["-e", name]
            prefix.append(self.container)
        else:
            prefix = []
        return [*prefix, program, *args], env

    def run(self, program, args, *, database=None, stdin=None, stdout=None):
        command, env = self.command(program, args, database)
        result = subprocess.run(command, env=env, stdin=stdin, stdout=stdout or subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=7200, check=False)
        if result.returncode:
            # pg tools may include customer data / connection details in stderr.
            raise BackupError(f"{program} failed (exit {result.returncode}); check connectivity, client version and database privileges")
        return result.stdout

    def sql(self, sql, database=None):
        return self.run("psql", ["-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-c", sql], database=database).decode().strip()

    def inventory(self, snapshot=None, database=None):
        begin = "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;"
        if snapshot:
            if not re.fullmatch(r"[0-9A-Fa-f-]+", snapshot):
                raise BackupError("Invalid exported snapshot")
            begin += f"SET TRANSACTION SNAPSHOT '{snapshot}';"
        rows = json.loads(self.sql(begin + "SELECT coalesce(json_agg(x),'[]'::json) FROM "
            "(SELECT schemaname,tablename FROM pg_tables WHERE schemaname NOT IN "
            "('pg_catalog','information_schema') ORDER BY 1,2) x; COMMIT;", database))
        queries = []
        for row in rows:
            schema, table = row["schemaname"], row["tablename"]
            label = json.dumps([schema, table]).replace("'", "''")
            queries.append(f"SELECT '{label}' AS name,count(*) AS count FROM {quoted(schema)}.{quoted(table)}")
        if not queries:
            raise BackupError("No application tables found")
        counts = self.sql(begin + "SELECT json_object_agg(name,count) FROM (" + " UNION ALL ".join(queries) + ") x; COMMIT;", database)
        return json.loads(counts)


# 先核对文件和附件哈希，再恢复到本函数生成的临时库并逐表核对行数，最后清理临时库。
def verify(pg, directory):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1:
        raise BackupError("Unsupported backup format")
    files = manifest["sha256"]
    if set(files) != {"database.dump", "attachments.tar.gz"}:
        raise BackupError("Invalid backup file list")
    for filename, expected in files.items():
        if sha256(directory / filename) != expected:
            raise BackupError("Backup checksum mismatch: " + filename)
    # Stream and hash archive members without extracting arbitrary archive paths.
    restored_files = archive_hashes(directory / "attachments.tar.gz")
    if restored_files != manifest["attachments"]:
        raise BackupError("Attachment content mismatch")
    scratch = "collector_restore_verify_" + uuid4().hex
    if not re.fullmatch(r"collector_restore_verify_[0-9a-f]{32}", scratch):
        raise BackupError("Invalid scratch name")
    created = False
    try:
        pg.sql(f"CREATE DATABASE {quoted(scratch)} TEMPLATE template0")
        created = True
        with (directory / "database.dump").open("rb") as dump:
            pg.run("pg_restore", ["--exit-on-error", "--no-owner", "--no-privileges", "--dbname=" + scratch],
                   database=scratch, stdin=dump)
        restored = pg.inventory(database=scratch)
        if restored != manifest["table_counts"]:
            raise BackupError("Restored table inventory / row counts do not match snapshot")
    finally:
        if created:
            pg.sql(f"DROP DATABASE {quoted(scratch)} WITH (FORCE)")
    result = {"status": "passed", "verified_at": datetime.now(UTC).isoformat(),
              "tables": len(restored), "attachment_files": len(restored_files), "scratch_database_removed": True}
    (directory / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


# pg_dump 与行数清单共用同一个数据库快照；仅在真实恢复验证通过后更新 latest.json。
def backup(pg, root, attachments, attachment_container=None):
    if not attachment_container and not attachments.is_dir():
        raise BackupError("Attachment directory does not exist")
    root = root.resolve()
    source = attachments.resolve()
    if not attachment_container and (root == source or root.is_relative_to(source)):
        raise BackupError("Backup output must be outside the attachment directory")
    directory = root / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    # Keep the exporting transaction open until pg_dump and all counts have completed.
    command, env = pg.command("psql", ["-X", "-qAt", "-v", "ON_ERROR_STOP=1"])
    keeper = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True)
    try:
        keeper.stdin.write("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SELECT pg_export_snapshot();\n")
        keeper.stdin.flush()
        snapshot = keeper.stdout.readline().strip()
        if not re.fullmatch(r"[0-9A-Fa-f-]+", snapshot):
            raise BackupError("Could not export database snapshot")
        counts = pg.inventory(snapshot=snapshot)
        with (directory / "database.dump").open("wb") as dump:
            pg.run("pg_dump", ["--format=custom", "--no-owner", "--no-privileges", "--snapshot=" + snapshot], stdout=dump)
    finally:
        keeper.communicate("ROLLBACK;\n", timeout=30)
    file_hashes = {}
    if attachment_container:
        with (directory / "attachments.tar.gz").open("wb") as output:
            result = subprocess.run(["docker", "exec", attachment_container, "tar", "czf", "-", "-C", attachments.as_posix(), "."],
                                    stdout=output, stderr=subprocess.PIPE, timeout=7200, check=False)
        if result.returncode:
            raise BackupError("Attachment container archive failed")
        file_hashes = archive_hashes(directory / "attachments.tar.gz")
    else:
        with tarfile.open(directory / "attachments.tar.gz", "w:gz") as archive:
            for path in sorted(source.rglob("*")):
                if path.is_symlink():
                    raise BackupError("Attachment symlinks are not supported")
                if path.is_file():
                    name = path.relative_to(source).as_posix()
                    before = sha256(path)
                    archive.add(path, arcname=name, recursive=False)
                    if sha256(path) != before:
                        raise BackupError("Attachment changed during backup; retry")
                    file_hashes[name] = before
    manifest = {"format": 1, "created_at": datetime.now(UTC).isoformat(), "table_counts": counts,
                "attachments": file_hashes,
                "sha256": {name: sha256(directory / name) for name in ("database.dump", "attachments.tar.gz")}}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    # A backup is not advertised as successful until a real restore has passed.
    result = verify(pg, directory)
    (root / "latest.json").write_text(json.dumps({"directory": str(directory), **result}, indent=2), encoding="utf-8")
    return {"directory": str(directory), **result}


def main():
    from app.config.settings import get_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "verify"])
    parser.add_argument("--pg-container", help="Use pg tools inside this Docker PostgreSQL container")
    parser.add_argument("--output", type=Path, default=Path("backups"))
    parser.add_argument("--attachments", type=Path)
    parser.add_argument("--attachments-container", help="Read attachment path inside this Docker container")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    if args.action == "backup" and args.attachments is None:
        parser.error("--attachments is required; use the API upload storage directory")
    if args.action == "verify" and args.backup_dir is None:
        parser.error("--backup-dir is required")
    try:
        pg = Postgres(get_settings().database_url, args.pg_container)
        result = backup(pg, args.output, args.attachments, args.attachments_container) if args.action == "backup" else verify(pg, args.backup_dir)
        print(json.dumps(result))
    except Exception as exc:  # noqa: BLE001 - redact connection details from all failures
        print(json.dumps({"status": "failed", "error": str(exc) if isinstance(exc, BackupError) else type(exc).__name__}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
