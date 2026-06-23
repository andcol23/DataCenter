-- ============================================================
-- Migración 003 — Programación de posts + vista de estadísticas pipeline
-- Ejecutar después de 002_sync_sources_and_post_sources.sql
-- ============================================================

-- 1) Campo de fecha programada para publicación (opcional)
--    Permite programar un post para publicar en fecha futura
ALTER TABLE linkedin_posts
    ADD COLUMN IF NOT EXISTS scheduled_for TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_posts_scheduled
    ON linkedin_posts(scheduled_for)
    WHERE scheduled_for IS NOT NULL;

-- 2) Campo source_type en la vista de candidatos pendientes para
--    distinguir artículos RSS / Gmail / GDrive en el generator
DROP VIEW IF EXISTS v_pending_post_candidates;

CREATE VIEW v_pending_post_candidates AS
SELECT
    ai.id AS analyzed_item_id,
    ri.title,
    ri.url,
    ri.published_at,
    s.name  AS source_name,
    s.type  AS source_type,
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
JOIN sources   s  ON s.id  = ri.source_id
WHERE ai.relevance_score >= 0.60
  AND ai.primary_slug IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM linkedin_posts lp
      WHERE lp.analyzed_item_id = ai.id
        AND lp.status != 'discarded'
  )
ORDER BY ai.relevance_score DESC, ai.analyzed_at DESC;

-- 3) Vista de estadísticas diarias del pipeline (útil para monitoring)
CREATE OR REPLACE VIEW v_pipeline_daily_stats AS
SELECT
    DATE_TRUNC('day', created_at AT TIME ZONE 'Europe/Madrid') AS day,
    COUNT(*)                                                    AS raw_items_received,
    COUNT(*) FILTER (WHERE status = 'analyzed')                 AS analyzed_ok,
    COUNT(*) FILTER (WHERE status = 'failed')                   AS analyzed_failed,
    COUNT(*) FILTER (WHERE status = 'raw')                      AS pending_analysis,
    ROUND(AVG(
        CASE WHEN status = 'analyzed'
             THEN (SELECT ai2.relevance_score
                   FROM analyzed_items ai2
                   WHERE ai2.raw_item_id = raw_items.id
                   LIMIT 1)
        END
    )::numeric, 2) AS avg_relevance_score
FROM raw_items
GROUP BY 1
ORDER BY 1 DESC;

-- 4) Vista de resumen de posts por estado y formato
CREATE OR REPLACE VIEW v_post_status_summary AS
SELECT
    status,
    format,
    COUNT(*)                                        AS total,
    MAX(created_at)                                 AS latest_created,
    ROUND(AVG(tokens_used)::numeric, 0)             AS avg_tokens
FROM linkedin_posts
GROUP BY status, format
ORDER BY
    CASE status
        WHEN 'draft'     THEN 1
        WHEN 'approved'  THEN 2
        WHEN 'published' THEN 3
        WHEN 'discarded' THEN 4
    END,
    format;

-- 5) Índice para consultas frecuentes del reviewer (filtra por created_at + status)
CREATE INDEX IF NOT EXISTS idx_posts_status_created
    ON linkedin_posts(status, created_at DESC);
