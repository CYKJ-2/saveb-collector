import io
import json
import tarfile

import pytest

from scripts.backup_restore import BackupError, Postgres, archive_hashes, sha256, verify


def test_credentials_not_in_command_and_restore_keeps_connection():
    pg = Postgres("postgresql://user:secret%21@db:5432/business", "pg-container")
    args, env = pg.command("pg_restore", ["--dbname=scratch"], "scratch")
    assert "secret" not in " ".join(args)
    assert env["PGDATABASE"] == "scratch"
    assert env["PGPASSWORD"] == "secret!"
    assert env["PGHOST"] == "db"


def test_unsafe_attachment_archive_rejected(tmp_path):
    path = tmp_path / "files.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        entry = tarfile.TarInfo("../outside")
        entry.size = 1
        archive.addfile(entry, io.BytesIO(b"a"))
    with pytest.raises(BackupError, match="Unsafe"):
        archive_hashes(path)


def test_corrupt_backup_never_creates_database(tmp_path):
    (tmp_path / "database.dump").write_bytes(b"original")
    (tmp_path / "attachments.tar.gz").write_bytes(b"original")
    manifest = {"format": 1, "sha256": {name: sha256(tmp_path / name) for name in ("database.dump", "attachments.tar.gz")}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "database.dump").write_bytes(b"corrupted")
    with pytest.raises(BackupError, match="checksum"):
        verify(None, tmp_path)


def test_restore_cleanup_runs_on_failure(tmp_path):
    (tmp_path / "database.dump").write_bytes(b"dump")
    with tarfile.open(tmp_path / "attachments.tar.gz", "w:gz"):
        pass
    manifest = {"format": 1, "attachments": {}, "table_counts": {},
                "sha256": {name: sha256(tmp_path / name) for name in ("database.dump", "attachments.tar.gz")}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    class FailedRestore:
        def __init__(self):
            self.statements = []
        def sql(self, sql):
            self.statements.append(sql)
        def run(self, *args, **kwargs):
            raise BackupError("restore failed")
    pg = FailedRestore()
    with pytest.raises(BackupError, match="restore failed"):
        verify(pg, tmp_path)
    assert pg.statements[0].startswith('CREATE DATABASE "collector_restore_verify_')
    assert pg.statements[1].startswith('DROP DATABASE "collector_restore_verify_')
    assert not (tmp_path / "verification.json").exists()
