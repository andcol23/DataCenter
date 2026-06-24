ALTER TABLE analyzed_items
  ADD COLUMN IF NOT EXISTS kpi_primary_value  NUMERIC(12,2),
  ADD COLUMN IF NOT EXISTS kpi_primary_unit   TEXT,
  ADD COLUMN IF NOT EXISTS kpi_primary_claim  TEXT,
  ADD COLUMN IF NOT EXISTS narrative_type     TEXT,
  ADD COLUMN IF NOT EXISTS data_strength      NUMERIC(3,2),
  ADD COLUMN IF NOT EXISTS brand_relevance    TEXT;


CREATE INDEX IF NOT EXISTS idx_analyzed_narrative_type
  ON analyzed_items(narrative_type);

CREATE INDEX IF NOT EXISTS idx_analyzed_brand_relevance
  ON analyzed_items(brand_relevance);

CREATE INDEX IF NOT EXISTS idx_analyzed_data_strength
  ON analyzed_items(data_strength DESC NULLS LAST);

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
    ai.brand_relevance
FROM analyzed_items ai
JOIN raw_items ri ON ri.id = ai.raw_item_id
JOIN sources s    ON s.id = ri.source_id
WHERE ai.relevance_score >= 0.60
  AND (ai.brand_relevance IS NULL OR ai.brand_relevance != 'peripheral')
  AND NOT EXISTS (
      SELECT 1 FROM linkedin_posts lp
      WHERE lp.analyzed_item_id = ai.id
        AND lp.status != 'discarded'
  )
ORDER BY
    ai.data_strength DESC NULLS LAST,
    ai.relevance_score DESC,
    ai.analyzed_at DESC;
