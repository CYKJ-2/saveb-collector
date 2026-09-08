CREATE TABLE IF NOT EXISTS collector.schedules (
    account text PRIMARY KEY,
    interval_minutes integer NOT NULL DEFAULT 30 CHECK (interval_minutes BETWEEN 5 AND 1440),
    next_run_at timestamptz NOT NULL DEFAULT (
        date_trunc('hour', now())
        + (floor(extract(minute from now()) / 30)::integer + 1) * interval '30 minutes'
    ),
    updated_by text NOT NULL DEFAULT 'system',
    updated_at timestamptz NOT NULL DEFAULT now()
);
