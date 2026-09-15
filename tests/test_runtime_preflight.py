import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'runtime_preflight', Path(__file__).parents[1] / 'scripts/runtime_preflight.py'
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DeploymentConfigTests(unittest.TestCase):
    def config(self):
        return dict(
            DB_DATABASE='saveb', DB_USERNAME='saveb', DB_PASSWORD='a@b',
            SAVEB_DATABASE_URL='postgresql://saveb:a%40b@postgres:5432/saveb',
            SAVEB_COLLECTOR_TOKEN='x' * 32, SAVEB_API_TOKEN='x' * 32,
            APP_ENV='test', SAVEB_APP_ENV='test',
            SAVEB_REDIS_URL='redis://saveb-collector-redis:6379/0',
            SAVEB_CELERY_BROKER_URL='redis://saveb-collector-redis:6379/1',
            SAVEB_CELERY_RESULT_BACKEND='redis://saveb-collector-redis:6379/2',
        )

    def test_url_encoded_password(self):
        with patch.dict(os.environ, self.config(), clear=True):
            self.assertEqual(module.main(), 0)

    def test_mismatched_token(self):
        config = self.config()
        config['SAVEB_API_TOKEN'] = 'y' * 32
        with patch.dict(os.environ, config, clear=True):
            self.assertEqual(module.main(), 65)

    def test_different_database_host(self):
        config = self.config()
        config['SAVEB_DATABASE_URL'] = 'postgresql://saveb:a%40b@production-db:5432/saveb'
        with patch.dict(os.environ, config, clear=True):
            self.assertEqual(module.main(), 65)

    def test_template_secret(self):
        config = self.config()
        config['DB_PASSWORD'] = 'REPLACE_WITH_DATABASE_PASSWORD'
        with patch.dict(os.environ, config, clear=True):
            self.assertEqual(module.main(), 65)
