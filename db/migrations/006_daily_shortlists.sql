CREATE TABLE IF NOT EXISTS daily_shortlists (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date_key        DATE NOT NULL DEFAULT CURRENT_DATE,
    items           JSONB NOT NULL DEFAULT '[]',
    email_thread_id TEXT,
    approved_nums   INTEGER[],
    reply_at        TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'sent',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (date_key)
);





CREATE INDEX IF NOT EXISTS idx_shortlists_date ON daily_shortlists(date_key DESC);
