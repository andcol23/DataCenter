-- ============================================================
-- Migración 005 — Score de novedad/recencia en analyzed_items
-- Ejecutar en Supabase SQL Editor después de 004
-- ============================================================
-- El producto prioriza lo más nuevo posible, incluido un dato nuevo sobre un tema
-- antiguo. novelty_score (0-1) captura esa frescura, separada de la relevancia.

ALTER TABLE analyzed_items
  ADD COLUMN IF NOT EXISTS novelty_score NUMERIC(3,2);

COMMENT ON COLUMN analyzed_items.novelty_score IS
  'Frescura del contenido: 1.0 noticia/dato de hoy, 0.7 dato nuevo de tema viejo, 0.0 atemporal';

CREATE INDEX IF NOT EXISTS idx_analyzed_novelty
  ON analyzed_items(novelty_score DESC NULLS LAST);

-- Actualizar la vista de candidatos para exponer novelty_score y ordenar por
-- una combinación de recencia + relevancia + fuerza de datos.
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
    ai.analyzed_at,
    ai.kpi_primary_value,
    ai.kpi_primary_unit,
    ai.kpi_primary_claim,
    ai.narrative_type,
    ai.data_strength,
    ai.brand_relevance,
    ai.novelty_score
FROM analyzed_items ai
JOIN raw_items ri ON ri.id = ai.raw_item_id
JOIN sources   s  ON s.id = ri.source_id
WHERE ai.relevance_score >= 0.60
  AND (ai.brand_relevance IS NULL OR ai.brand_relevance != 'peripheral')
  AND NOT EXISTS (
      SELECT 1 FROM linkedin_posts lp
      WHERE lp.analyzed_item_id = ai.id
        AND lp.status != 'discarded'
  )
ORDER BY
    COALESCE(ai.novelty_score, 0)   DESC,
    ai.relevance_score              DESC,
    COALESCE(ai.data_strength, 0)   DESC,
    ai.analyzed_at                  DESC;
