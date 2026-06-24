-- ============================================================
-- Migración 006 — Tabla daily_shortlists
-- Ejecutar en Supabase SQL Editor después de 005
-- ============================================================
-- Registra el shortlist de temas enviado cada mañana por email,
-- y la respuesta del revisor con los números aprobados.

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

COMMENT ON TABLE daily_shortlists IS
    'Un registro por día: shortlist enviado, hilo de Gmail y números aprobados por el revisor.';

COMMENT ON COLUMN daily_shortlists.items IS
    'Array JSON de {num, analyzed_item_id, title, primary_slug, source_name, ...}';

COMMENT ON COLUMN daily_shortlists.approved_nums IS
    'Números que el revisor respondió por email (ej: {2,4})';

COMMENT ON COLUMN daily_shortlists.status IS
    'sent | replied | generated | expired';

CREATE INDEX IF NOT EXISTS idx_shortlists_date ON daily_shortlists(date_key DESC);
