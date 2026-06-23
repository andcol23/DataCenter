-- ============================================================
-- Migración 001 — Columnas de taxonomía en analyzed_items
-- Ejecutar en Supabase SQL Editor (una sola vez)
-- ============================================================

ALTER TABLE analyzed_items
  ADD COLUMN IF NOT EXISTS primary_slug   TEXT,
  ADD COLUMN IF NOT EXISTS secondary_slug TEXT,
  ADD COLUMN IF NOT EXISTS keywords       JSONB NOT NULL DEFAULT '[]';

-- Índices para filtrado y agrupación por taxonomía
CREATE INDEX IF NOT EXISTS idx_analyzed_primary_slug
  ON analyzed_items(primary_slug);

CREATE INDEX IF NOT EXISTS idx_analyzed_secondary_slug
  ON analyzed_items(primary_slug, secondary_slug);

CREATE INDEX IF NOT EXISTS idx_analyzed_keywords
  ON analyzed_items USING gin(keywords);

-- Actualizar la vista para incluir las nuevas columnas
-- (DROP + CREATE porque Postgres no soporta ALTER VIEW para añadir columnas)
DROP VIEW IF EXISTS v_pending_post_candidates;

CREATE VIEW v_pending_post_candidates AS
SELECT
    ai.id               AS analyzed_item_id,
    ri.title,
    ri.url,
    ri.published_at,
    s.name              AS source_name,
    s.type              AS source_type,
    ai.summary,
    ai.key_insights,
    ai.topics,
    ai.primary_slug,
    ai.secondary_slug,
    ai.keywords,
    ai.sentiment,
    ai.relevance_score,
    ai.linkedin_angle,
    ai.analyzed_at
FROM analyzed_items ai
JOIN raw_items ri ON ri.id = ai.raw_item_id
JOIN sources s    ON s.id = ri.source_id
WHERE ai.relevance_score >= 0.60
  AND NOT EXISTS (
      SELECT 1 FROM linkedin_posts lp
      WHERE lp.analyzed_item_id = ai.id
        AND lp.status != 'discarded'
  )
ORDER BY ai.relevance_score DESC, ai.analyzed_at DESC;
