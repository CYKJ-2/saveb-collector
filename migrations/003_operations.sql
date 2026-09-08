CREATE TABLE IF NOT EXISTS collector.pending_state (
 account text PRIMARY KEY, cursor_day date NOT NULL,
 next_run_at timestamptz NOT NULL DEFAULT now(), last_job_id text,
 last_completed_at timestamptz, cycles_completed integer NOT NULL DEFAULT 0
);
-- Pending-only coverage must NEVER imply complete date coverage for missing-mode jobs.
CREATE TABLE IF NOT EXISTS collector.pending_coverage (
 account text NOT NULL, start_day date NOT NULL, end_day date NOT NULL,
 job_id text NOT NULL REFERENCES collector.jobs(id), fetched integer NOT NULL,
 completed_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(account,start_day,end_day)
);
CREATE TABLE IF NOT EXISTS collector.reconciliation_reports (
 id bigserial PRIMARY KEY, account text NOT NULL, job_id text REFERENCES collector.jobs(id),
 chunk_id bigint REFERENCES collector.chunks(id), kind text NOT NULL,
 start_day date, end_day date, status text NOT NULL, report jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS collector_reconciliation_created ON collector.reconciliation_reports(created_at);
INSERT INTO collector.schema_versions(version) VALUES(3) ON CONFLICT DO NOTHING;
