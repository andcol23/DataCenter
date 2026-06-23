-- ============================================================
-- Migración 007 — Tabla shortlist_items (aprobación por casilla)
-- Ejecutar en Supabase SQL Editor DESPUÉS de 006_daily_shortlists.sql
-- ============================================================
-- Cada tema del shortlist diario tiene su propia fila con un
-- campo approved (boolean) que André activa en el Table Editor.
-- El job morning inserta con approved=false; el job evening lee
-- solo las filas donde approved=true.

CREATE TABLE IF NOT EXISTS shortlist_items (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    shortlist_id     UUID        REFERENCES daily_shortlists(id) ON DELETE CASCADE,
    date_key         DATE        NOT NULL DEFAULT CURRENT_DATE,
    num              SMALLINT    NOT NULL,
    analyzed_item_id UUID        NOT NULL,
    title            TEXT        NOT NULL DEFAULT '',
    primary_slug     TEXT        NOT NULL DEFAULT '',
    secondary_slug   TEXT,
    source_name      TEXT,
    summary          TEXT,
    shortlist_reason TEXT,
    approved         BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (date_key, analyzed_item_id)
);

COMMENT ON TABLE shortlist_items IS
    'Un registro por tema del shortlist diario. André marca la casilla approved para publicar.';

COMMENT ON COLUMN shortlist_items.approved IS
    'TRUE si André quiere publicar este tema. El job de las 20:00 lee solo filas con approved=true.';

COMMENT ON COLUMN shortlist_items.num IS
    'Posición del tema en el shortlist del día (1 = más prioritario).';

CREATE INDEX IF NOT EXISTS idx_shortlist_items_date
    ON shortlist_items(date_key DESC);

CREATE INDEX IF NOT EXISTS idx_shortlist_items_approved
    ON shortlist_items(date_key, approved)
    WHERE approved = TRUE;

-- ── Vista para el Table Editor ────────────────────────────────
-- Filtra por hoy y ordena por número para facilitar la revisión.
CREATE OR REPLACE VIEW v_shortlist_today AS
SELECT
    id,
    num,
    title,
    primary_slug,
    secondary_slug,
    source_name,
    summary,
    shortlist_reason,
    approved,
    date_key
FROM shortlist_items
WHERE date_key = CURRENT_DATE
ORDER BY num;

COMMENT ON VIEW v_shortlist_today IS
    'Temas del shortlist de HOY, ordenados por número. Marca la casilla approved para publicar.';
