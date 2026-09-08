"""Exercise the real release script with a fake Docker CLI; never touches a business database."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("release.sh")
OLD = "a" * 40
NEW = "b" * 40


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="saveb-release-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "app"
        self.root.mkdir()
        (self.root / ".env").write_text("EXAMPLE=true\n")
        self.env = {**os.environ, "MOCK_LOG": str(self.root / "docker.log")}
        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text("""#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
image = os.environ.get('RELEASE_IMAGE', '')
with open(os.environ['MOCK_LOG'], 'a') as f:
    f.write(' '.join(args) + ' IMAGE=' + image + '\\n')
if 'run' in args and os.environ.get('FAIL_MIGRATION') == '1':
    sys.exit(1)
if 'up' in args and os.environ.get('FAIL_IMAGE') and os.environ['FAIL_IMAGE'] in image:
    sys.exit(1)
""")
        docker.chmod(0o755)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]
        for sha, digit in ((OLD, "1"), (NEW, "2")):
            release = self.root / "releases" / sha
            release.mkdir(parents=True)
            shutil.copyfile(SCRIPT, release / "release.sh")
            (release / "app.id").write_text("saveb-api")
            (release / "compose.yml").write_text("services: {}\n")
            (release / "image.ref").write_text("ghcr.io/example/saveb-api@sha256:" + digit * 64)

    def run_release(self, sha, mode="deploy"):
        return subprocess.run(
            ["bash", str(self.root / "releases" / sha / "release.sh"), str(self.root), sha, mode],
            env=self.env, text=True, capture_output=True,
        )

    def seed(self):
        self.assertEqual(self.run_release(OLD).returncode, 0)

    def test_success_and_manual_rollback(self):
        self.seed()
        self.assertEqual(self.run_release(NEW).returncode, 0)
        self.assertEqual((self.root / "current").read_text().strip(), NEW)
        self.assertEqual(self.run_release(OLD, "rollback").returncode, 0)
        self.assertEqual((self.root / "current").read_text().strip(), OLD)

    def test_health_failure_restores_previous_and_reports_failure(self):
        self.seed()
        self.env["FAIL_IMAGE"] = "2" * 64
        self.assertNotEqual(self.run_release(NEW).returncode, 0)
        self.assertEqual((self.root / "current").read_text().strip(), OLD)
        self.assertFalse((self.root / "pending").exists())
        self.assertIn("rolled_back", (self.root / "releases.log").read_text())

    def test_first_failure_has_no_false_success(self):
        self.env["FAIL_IMAGE"] = "2" * 64
        self.assertNotEqual(self.run_release(NEW).returncode, 0)
        self.assertFalse((self.root / "current").exists())
        self.assertTrue((self.root / "pending").exists())

    def test_migration_failure_does_not_switch_or_reverse_schema(self):
        self.seed()
        (self.root / "releases" / NEW / "migrate.required").touch()
        hook = self.root / "pre-migrate.sh"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
        self.env["FAIL_MIGRATION"] = "1"
        self.assertNotEqual(self.run_release(NEW).returncode, 0)
        self.assertEqual((self.root / "current").read_text().strip(), OLD)
        self.assertNotIn("migrate:rollback", (self.root / "docker.log").read_text())

    def test_backup_hook_required(self):
        self.seed()
        (self.root / "releases" / NEW / "migrate.required").touch()
        self.assertNotEqual(self.run_release(NEW).returncode, 0)
        self.assertEqual((self.root / "current").read_text().strip(), OLD)

    def test_unhealthy_version_cannot_be_selected_for_rollback(self):
        self.seed()
        self.assertNotEqual(self.run_release(NEW, "rollback").returncode, 0)
        self.assertEqual((self.root / "current").read_text().strip(), OLD)

    def test_interrupted_switch_recovers_recorded_current(self):
        self.seed()
        (self.root / "pending").write_text(NEW)
        self.assertEqual(self.run_release(NEW).returncode, 0)
        self.assertIn("recovered_previous", (self.root / "releases.log").read_text())

    def test_invalid_revision_refused(self):
        result = subprocess.run(["bash", str(SCRIPT), str(self.root), "../bad"], env=self.env)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
