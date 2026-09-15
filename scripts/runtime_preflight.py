"""Check deployment configuration without network access or printing credentials."""
import os
import sys
from urllib.parse import unquote, urlsplit


def main():
    errors = []
    env = os.environ
    for key in ('DB_DATABASE', 'DB_USERNAME', 'DB_PASSWORD', 'SAVEB_COLLECTOR_TOKEN', 'SAVEB_API_TOKEN', 'SAVEB_DATABASE_URL'):
        value = env.get(key, '')
        if not value or 'REPLACE_' in value or 'CHANGE_ME' in value:
            errors.append(f'{key}: configure a real value')
    if env.get('SAVEB_COLLECTOR_TOKEN') != env.get('SAVEB_API_TOKEN'):
        errors.append('Collector tokens do not match')
    if len(env.get('SAVEB_COLLECTOR_TOKEN', '')) < 32:
        errors.append('Collector token must contain at least 32 characters')
    try:
        db = urlsplit(env.get('SAVEB_DATABASE_URL', ''))
        if (db.hostname != 'postgres' or db.port not in (None, 5432)
                or unquote(db.username or '') != env.get('DB_USERNAME')
                or unquote(db.password or '') != env.get('DB_PASSWORD')
                or unquote(db.path.lstrip('/')) != env.get('DB_DATABASE')):
            errors.append('Collector database connection must match the API database (host postgres)')
        for key in ('SAVEB_REDIS_URL', 'SAVEB_CELERY_BROKER_URL', 'SAVEB_CELERY_RESULT_BACKEND'):
            if urlsplit(env.get(key, '')).hostname != 'saveb-collector-redis':
                errors.append(f'{key}: use the private collector Redis service')
    except ValueError:
        errors.append('Invalid connection URL')
    if env.get('APP_ENV') != env.get('SAVEB_APP_ENV'):
        errors.append('API and Collector environments must match')
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 65
    print('Deployment configuration checked; no credentials displayed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
