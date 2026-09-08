CREATE TABLE IF NOT EXISTS collector.logistics_schedule (
 account text PRIMARY KEY, next_run_at timestamptz NOT NULL DEFAULT now(),
 last_job_id text, updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO collector.schema_versions(version) VALUES(5) ON CONFLICT DO NOTHING;
