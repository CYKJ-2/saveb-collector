DO $$ BEGIN
 IF to_regclass('public.orders') IS NOT NULL AND NOT EXISTS (
   SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name='orders' AND column_name='source_created_at'
 ) THEN
   RAISE EXCEPTION 'Run saveb-api migration 2026_09_08_120000_separate_order_source_times first';
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS collector.order_date_repairs (
 id bigserial PRIMARY KEY, order_id text NOT NULL, before_value jsonb NOT NULL,
 after_value jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO collector.schema_versions(version) VALUES(4) ON CONFLICT DO NOTHING;
