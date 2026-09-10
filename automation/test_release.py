"""Run release/backup scripts against a fake Docker CLI in disposable directories."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

OLD, NEW = "a" * 40, "b" * 40
SOURCE = Path(__file__).resolve().parent


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="saveb-release-test-")
        self.addCleanup(self.tmp.cleanup)
        self.www = Path(self.tmp.name)
        self.root = self.www / "saveb-api"
        self.root.mkdir()
        (self.root / ".env").write_text("EXAMPLE=true\n")
        (self.root / ".deploy-ready").touch()
        (self.root / "docker-compose.infra.yml").write_text("services: {}\n")
        self.checkout = self.www / "checkout"
        self.scripts = self.checkout / "automation"
        self.scripts.mkdir(parents=True)
        for name in ("release.sh", "backup.sh"):
            shutil.copyfile(SOURCE / name, self.scripts / name)
        (self.scripts / "app.id").write_text("saveb-api\n")
        (self.checkout / "docker-compose.server.yml").write_text("services: {}\n")
        self.log = self.www / "docker.log"
        bin_dir = self.www / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text('''#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
image = os.environ.get('RELEASE_IMAGE', '')
with open(os.environ['MOCK_LOG'], 'a') as f:
    f.write(' '.join(args) + ' IMAGE=' + image + '\\n')
joined = ' '.join(args)
if 'SELECT count(*)' in joined:
    print('0' if os.environ.get('EMPTY_RBAC') else '1')
if 'pg_dump' in joined:
    if os.environ.get('FAIL_BACKUP'): sys.exit(1)
    print('fake-dump-content')
if 'pg_restore' in joined and os.environ.get('FAIL_ARCHIVE'): sys.exit(1)
if 'pull' in args and os.environ.get('FAIL_PULL'): sys.exit(1)
if 'run' in args and os.environ.get('FAIL_MIGRATION'): sys.exit(1)
if 'up' in args and os.environ.get('FAIL_IMAGE') in (image, 'all'):
    sys.exit(1)
''')
        docker.chmod(0o755)
        self.env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                    "MOCK_LOG": str(self.log), "SAVEB_WWW_ROOT": str(self.www)}

    def image(self, sha):
        return "ghcr.io/ding-cykj/saveb-api@sha256:" + ("1" if sha == OLD else "2") * 64

    def run_release(self, sha, mode="deploy", root=None, image=None):
        return subprocess.run(["bash", str(self.scripts / "release.sh"), str(root or self.root),
                               sha, mode, image or self.image(sha)],
                              env=self.env, capture_output=True, text=True)

    def succeed(self, sha=OLD):
        result = self.run_release(sha)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def text_log(self):
        return self.log.read_text() if self.log.exists() else ""

    def test_success_and_manual_rollback(self):
        self.succeed()
        self.succeed(NEW)
        self.assertEqual(self.run_release(OLD, "rollback").returncode, 0)
        self.assertEqual((self.root / "current").read_text().strip(), OLD)

    def test_health_failure_recovers_previous_and_reports_failure(self):
        self.succeed()
        self.env["FAIL_IMAGE"] = self.image(NEW)
        self.assertNotEqual(self.run_release(NEW).returncode, 0)
        self.assertEqual((self.root / "current").read_text().strip(), OLD)
        self.assertFalse((self.root / "pending").exists())
        self.assertIn("rolled_back", (self.root / "releases.log").read_text())

    def test_first_failure_retains_pending_and_never_reports_success(self):
        self.env["FAIL_IMAGE"] = self.image(NEW)
        self.assertNotEqual(self.run_release(NEW).returncode, 0)
        self.assertFalse((self.root / "current").exists())
        self.assertTrue((self.root / "pending").exists())
        self.assertNotEqual(self.run_release(NEW).returncode, 0)

    def test_migration_failure_does_not_activate(self):
        self.succeed()
        self.log.write_text("")
        self.env["FAIL_MIGRATION"] = "1"
        self.assertNotEqual(self.run_release(NEW).returncode, 0)
        self.assertNotIn(" up ", self.text_log())
        self.assertEqual((self.root / "current").read_text().strip(), OLD)

    def test_backup_and_archive_failures_prevent_migration(self):
        for flag in ("FAIL_BACKUP", "FAIL_ARCHIVE", "EMPTY_RBAC"):
            with self.subTest(flag=flag):
                self.log.write_text("")
                self.env[flag] = "1"
                self.assertNotEqual(self.run_release(NEW).returncode, 0)
                self.assertNotIn(" run ", self.text_log())
                self.assertNotIn(" up ", self.text_log())
                del self.env[flag]

    def test_interrupted_activation_recovers_recorded_version(self):
        self.succeed()
        (self.root / "pending").write_text(NEW)
        self.succeed(NEW)
        self.assertIn("recovered_previous", (self.root / "releases.log").read_text())

    def test_failed_recovery_keeps_pending(self):
        self.succeed()
        (self.root / "pending").write_text(NEW)
        self.env["FAIL_IMAGE"] = "all"
        self.assertNotEqual(self.run_release(NEW).returncode, 0)
        self.assertTrue((self.root / "pending").exists())

    def test_readiness_marker_required(self):
        (self.root / ".deploy-ready").unlink()
        self.assertNotEqual(self.run_release(NEW).returncode, 0)
        self.assertEqual(self.text_log(), "")

    def test_foreign_directory_revision_and_image_rejected(self):
        foreign = self.www / "another-project"
        foreign.mkdir()
        self.assertNotEqual(self.run_release(NEW, root=foreign).returncode, 0)
        self.assertNotEqual(self.run_release("../bad").returncode, 0)
        self.assertNotEqual(self.run_release(NEW, image="ghcr.io/other/app:latest").returncode, 0)
        self.assertEqual(self.text_log(), "")

    def test_same_commit_cannot_change_digest(self):
        self.succeed()
        self.assertNotEqual(self.run_release(OLD, image=self.image(NEW)).returncode, 0)

    def test_only_healthy_recorded_revision_can_rollback(self):
        self.succeed()
        self.assertNotEqual(self.run_release(NEW, "rollback").returncode, 0)
        self.env["FAIL_IMAGE"] = self.image(NEW)
        self.assertNotEqual(self.run_release(NEW).returncode, 0)
        del self.env["FAIL_IMAGE"]
        self.assertNotEqual(self.run_release(NEW, "rollback").returncode, 0)

    def test_rollback_skips_migration_and_backup(self):
        self.succeed()
        self.succeed(NEW)
        self.log.write_text("")
        self.assertEqual(self.run_release(OLD, "rollback").returncode, 0)
        self.assertNotIn(" run ", self.text_log())
        self.assertNotIn("pg_dump", self.text_log())

    def test_image_pull_failure_preserves_running_version(self):
        self.succeed()
        self.log.write_text("")
        self.env["FAIL_PULL"] = "1"
        self.assertNotEqual(self.run_release(NEW).returncode, 0)
        self.assertNotIn(" up ", self.text_log())
        self.assertNotIn(" run ", self.text_log())

    def test_never_builds_or_prunes_or_reverses_database(self):
        self.succeed()
        log = self.text_log()
        self.assertIn("--no-build --pull never --wait", log)
        for command in (" build ", "prune", " down ", "migrate:rollback", "migrate:fresh"):
            self.assertNotIn(command, log)
        self.assertLess(log.index("pg_dump"), log.index(" run "))
        self.assertLess(log.index("pg_restore"), log.index(" run "))
        self.assertTrue(list((self.www / "backups").glob("*/SHA256SUMS")))


if __name__ == "__main__":
    unittest.main()
