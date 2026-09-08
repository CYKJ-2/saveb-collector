CREATE SCHEMA IF NOT EXISTS collector;
CREATE TABLE IF NOT EXISTS collector.exchange_rates (
 effective_date date PRIMARY KEY, rates jsonb NOT NULL, source text NOT NULL,
 fetched_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS collector.jobs (
 id text PRIMARY KEY, account text NOT NULL, mode text NOT NULL,
 actor text NOT NULL, idempotency_key text NOT NULL, request_hash text NOT NULL,
 params jsonb NOT NULL, context jsonb NOT NULL, status text NOT NULL DEFAULT 'queued',
 cancel_requested boolean NOT NULL DEFAULT false, error text,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(account, actor, idempotency_key)
);
CREATE TABLE IF NOT EXISTS collector.chunks (
 id bigserial PRIMARY KEY, job_id text NOT NULL REFERENCES collector.jobs(id),
 scope jsonb NOT NULL, status text NOT NULL DEFAULT 'queued', attempts int NOT NULL DEFAULT 0,
 error text, counts jsonb NOT NULL DEFAULT '{}', raw jsonb, normalized jsonb,
 fetched_at timestamptz, committed_at timestamptz, UNIQUE(job_id, scope)
);
CREATE TABLE IF NOT EXISTS collector.outbox (
 job_id text PRIMARY KEY REFERENCES collector.jobs(id), next_dispatch_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS collector.source_orders (
 account text NOT NULL, order_id text NOT NULL, normalized jsonb NOT NULL,
 raw jsonb NOT NULL, content_hash text NOT NULL, source_updated_at timestamptz,
 last_seen_at timestamptz NOT NULL DEFAULT now(), job_id text NOT NULL,
 projected jsonb, PRIMARY KEY(account, order_id)
);
CREATE TABLE IF NOT EXISTS collector.coverage (
 account text NOT NULL, day date NOT NULL, job_id text NOT NULL,
 published boolean NOT NULL DEFAULT false,
 completed_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(account, day)
);
ALTER TABLE collector.coverage ADD COLUMN IF NOT EXISTS published boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS collector.checkpoints (account text PRIMARY KEY, history_cursor date NOT NULL);
CREATE TABLE IF NOT EXISTS collector.changes (
 id bigserial PRIMARY KEY, job_id text NOT NULL, order_id text NOT NULL,
 before_value jsonb, after_value jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS collector_chunks_jobs ON collector.chunks(job_id, status, id);
CREATE INDEX IF NOT EXISTS collector_jobs_status ON collector.jobs(status, created_at);
CREATE TABLE IF NOT EXISTS collector.schema_versions(version int PRIMARY KEY, installed_at timestamptz DEFAULT now());
INSERT INTO collector.schema_versions(version) VALUES (1) ON CONFLICT DO NOTHING;
CREATE SCHEMA IF NOT EXISTS collector;
CREATE TABLE IF NOT EXISTS collector.scheduler_state (
    account text PRIMARY KEY,
    last_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_success_at timestamptz,
    error text
);
