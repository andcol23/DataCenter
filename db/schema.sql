-- ============================================================
-- Media Intelligence Hub — Schema SQL
-- Requiere: pg_vector extension en Supabase
-- ============================================================

-- Habilitar extensión de vectores
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================================
-- ENUM TYPES
-- ============================================================

CREATE TYPE source_type AS ENUM ('rss', 'gmail', 'gdrive', 'obsidian', 'manual');
CREATE TYPE item_status AS ENUM ('raw', 'analyzing', 'analyzed', 'failed', 'archived');
CREATE TYPE post_format AS ENUM ('single_post', 'carousel', 'article');
CREATE TYPE post_status AS ENUM ('draft', 'approved', 'published', 'discarded');

-- ============================================================
-- TABLA: sources
-- Repositorio de todas las fuentes de contenido registradas
-- ============================================================

CREATE TABLE sources (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    type            source_type NOT NULL,
    config          JSONB NOT NULL DEFAULT '{}',
    -- Para RSS: {"url": "https://..."}
    -- Para Gmail: {"label": "media-hub", "sender_filter": "@substack.com"}
    -- Para GDrive: {"folder_id": "1abc..."}
    -- Para Obsidian: {"vault_path": "Inbox/"}
    is_active       BOOLEAN NOT NULL DEFAULT true,
    fetch_interval  INTERVAL NOT NULL DEFAULT '12 hours',
    last_fetched_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (name, type)         -- requerido por upsert_source ON CONFLICT (name, type)
);

-- ============================================================
-- TABLA: raw_items
-- Contenido crudo capturado antes del análisis
-- ============================================================

CREATE TABLE raw_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id       UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id     TEXT,                           -- GUID del feed / Message-ID del email / file_id de Drive
    url             TEXT,
    title           TEXT,
    body_text       TEXT,                           -- Contenido extraído (texto plano)
    body_html       TEXT,                           -- HTML original si aplica
    author          TEXT,
    published_at    TIMESTAMPTZ,
    metadata        JSONB NOT NULL DEFAULT '{}',   -- Datos extra específicos por fuente
    status          item_status NOT NULL DEFAULT 'raw',
    error_message   TEXT,                           -- Razón de fallo si status = 'failed'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Evitar duplicados por fuente
    UNIQUE (source_id, external_id)
);

CREATE INDEX idx_raw_items_source_id   ON raw_items(source_id);
CREATE INDEX idx_raw_items_status      ON raw_items(status);
CREATE INDEX idx_raw_items_published   ON raw_items(published_at DESC);

-- ============================================================
-- TABLA: analyzed_items
-- Resultado del análisis con OpenAI (extracción JSON estructurado)
-- ============================================================

CREATE TABLE analyzed_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    raw_item_id     UUID NOT NULL UNIQUE REFERENCES raw_items(id) ON DELETE CASCADE,

    -- Campos extraídos por el agente OpenAI
    summary         TEXT,                           -- Resumen ejecutivo (150-200 palabras)
    key_insights    TEXT[],                         -- Array de insights clave (3-5)
    topics          TEXT[],                         -- Temas principales detectados
    entities        JSONB NOT NULL DEFAULT '[]',    -- [{name, type: person|org|product|concept}]
    sentiment       TEXT,                           -- positive | neutral | negative | mixed
    relevance_score NUMERIC(3,2),                   -- 0.00 - 1.00, calculado por el agente
    content_type    TEXT,                           -- opinion | research | news | tutorial | interview
    target_audience TEXT,                           -- Audiencia estimada del artículo original
    linkedin_angle  TEXT,                           -- Ángulo sugerido para LinkedIn
    primary_slug    TEXT,                           -- Macrotema canónico
    secondary_slug  TEXT,                           -- Subtema canónico dentro del macrotema
    keywords        JSONB NOT NULL DEFAULT '[]',    -- ["keyword_type/keyword-slug", ...]
    raw_analysis    JSONB NOT NULL DEFAULT '{}',    -- JSON completo devuelto por OpenAI

    -- Embedding del contenido para búsqueda semántica
    embedding       vector(1536),                   -- text-embedding-3-small = 1536 dims

    analyzed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_used      TEXT NOT NULL DEFAULT 'gpt-4o-mini',
    tokens_used     INTEGER,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Índice HNSW para búsqueda semántica eficiente
CREATE INDEX idx_analyzed_embedding ON analyzed_items
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_analyzed_relevance  ON analyzed_items(relevance_score DESC);
CREATE INDEX idx_analyzed_topics     ON analyzed_items USING gin(topics);
CREATE INDEX idx_analyzed_primary    ON analyzed_items(primary_slug);
CREATE INDEX idx_analyzed_secondary  ON analyzed_items(primary_slug, secondary_slug);
CREATE INDEX idx_analyzed_keywords   ON analyzed_items USING gin(keywords);

-- ============================================================
-- TABLA: tags
-- Taxonomía controlada de etiquetas
-- ============================================================

CREATE TABLE tags (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    category    TEXT,                               -- technology | business | trend | person | etc.
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- TABLA: item_tags
-- Relación many-to-many entre analyzed_items y tags
-- ============================================================

CREATE TABLE item_tags (
    item_id     UUID NOT NULL REFERENCES analyzed_items(id) ON DELETE CASCADE,
    tag_id      UUID NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    confidence  NUMERIC(3,2) DEFAULT 1.00,         -- Confianza del tag (1.00 = manual)
    PRIMARY KEY (item_id, tag_id)
);

-- ============================================================
-- TABLA: linkedin_posts
-- Posts y carousels generados listos para publicar
-- ============================================================

CREATE TABLE linkedin_posts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    analyzed_item_id UUID REFERENCES analyzed_items(id) ON DELETE SET NULL,

    format          post_format NOT NULL,
    status          post_status NOT NULL DEFAULT 'draft',

    -- Contenido del post
    hook            TEXT,                           -- Primera línea/gancho
    body            TEXT NOT NULL,                  -- Cuerpo completo
    cta             TEXT,                           -- Call to action
    hashtags        TEXT[],

    -- Para carousels: array de slides [{title, body, visual_suggestion}]
    slides          JSONB,

    -- Métricas de calidad (opcionales, para tracking futuro)
    estimated_reach INTEGER,
    engagement_pred NUMERIC(5,2),

    -- Metadata de generación
    generated_by    TEXT NOT NULL DEFAULT 'gpt-4o-mini',
    generation_prompt TEXT,                         -- Prompt usado (auditoría)
    tokens_used     INTEGER,

    published_at    TIMESTAMPTZ,
    notes           TEXT,                           -- Notas manuales del editor

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_posts_status       ON linkedin_posts(status);
CREATE INDEX idx_posts_format       ON linkedin_posts(format);
CREATE INDEX idx_posts_created      ON linkedin_posts(created_at DESC);

-- ============================================================
-- TABLA: linkedin_post_sources
-- Trazabilidad many-to-many entre un post generado y los items analizados usados
-- ============================================================

CREATE TABLE linkedin_post_sources (
    linkedin_post_id UUID NOT NULL REFERENCES linkedin_posts(id) ON DELETE CASCADE,
    analyzed_item_id UUID NOT NULL REFERENCES analyzed_items(id) ON DELETE CASCADE,
    source_order     SMALLINT NOT NULL DEFAULT 1,
    is_primary       BOOLEAN NOT NULL DEFAULT false,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (linkedin_post_id, analyzed_item_id)
);

CREATE INDEX idx_post_sources_item  ON linkedin_post_sources(analyzed_item_id);
CREATE INDEX idx_post_sources_order ON linkedin_post_sources(linkedin_post_id, source_order);

-- ============================================================
-- TABLA: fetch_logs
-- Historial de ejecuciones del scheduler para debugging
-- ============================================================

CREATE TABLE fetch_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id       UUID REFERENCES sources(id) ON DELETE SET NULL,
    run_id          TEXT,                           -- ID del GitHub Actions run
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    items_found     INTEGER DEFAULT 0,
    items_new       INTEGER DEFAULT 0,
    items_failed    INTEGER DEFAULT 0,
    error_message   TEXT,
    success         BOOLEAN
);

CREATE INDEX idx_fetch_logs_source   ON fetch_logs(source_id);
CREATE INDEX idx_fetch_logs_started  ON fetch_logs(started_at DESC);

-- ============================================================
-- FUNCIÓN: updated_at automático
-- ============================================================

CREATE OR REPLACE FUNCTION trigger_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER set_updated_at BEFORE UPDATE ON sources
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

CREATE TRIGGER set_updated_at BEFORE UPDATE ON raw_items
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

CREATE TRIGGER set_updated_at BEFORE UPDATE ON analyzed_items
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

CREATE TRIGGER set_updated_at BEFORE UPDATE ON linkedin_posts
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- ============================================================
-- FUNCIÓN: búsqueda semántica por similitud coseno
-- Uso: SELECT * FROM search_similar_items('tu query embebida', 0.75, 10);
-- ============================================================

CREATE OR REPLACE FUNCTION search_similar_items(
    query_embedding vector(1536),
    similarity_threshold FLOAT DEFAULT 0.70,
    match_count INT DEFAULT 20
)
RETURNS TABLE (
    id              UUID,
    raw_item_id     UUID,
    summary         TEXT,
    key_insights    TEXT[],
    topics          TEXT[],
    relevance_score NUMERIC(3,2),
    similarity      FLOAT
)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    SELECT
        ai.id,
        ai.raw_item_id,
        ai.summary,
        ai.key_insights,
        ai.topics,
        ai.relevance_score,
        1 - (ai.embedding <=> query_embedding) AS similarity
    FROM analyzed_items ai
    WHERE ai.embedding IS NOT NULL
      AND 1 - (ai.embedding <=> query_embedding) >= similarity_threshold
    ORDER BY ai.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;

-- ============================================================
-- VISTA: dashboard de items pendientes de post
-- ============================================================

CREATE VIEW v_pending_post_candidates AS
SELECT
    ai.id AS analyzed_item_id,
    ri.title,
    ri.url,
    ri.published_at,
    s.name AS source_name,
    s.type AS source_type,
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

-- ============================================================
-- TABLA: daily_shortlists
-- Guarda el shortlist diario enviado por email y la respuesta de aprobación
-- ============================================================

CREATE TABLE daily_shortlists (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date_key        TEXT NOT NULL UNIQUE,           -- "YYYY-MM-DD" — una fila por día
    items           JSONB NOT NULL DEFAULT '[]',    -- array de candidatos numerados
    email_thread_id TEXT,                           -- threadId de Gmail para leer la respuesta
    status          TEXT NOT NULL DEFAULT 'sent',   -- sent | replied | generated | skipped
    approved_nums   INTEGER[],                      -- números aprobados (ej: [2, 4])
    reply_at        TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER set_updated_at BEFORE UPDATE ON daily_shortlists
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- ============================================================
-- TABLA: on_demand_requests
-- Solicitudes on-demand de generación de posts (trigger por email)
-- ============================================================

CREATE TABLE on_demand_requests (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gmail_message_id    TEXT UNIQUE,                -- para deduplicar
    request_text        TEXT,                       -- asunto / cuerpo original
    request_type        TEXT,                       -- 'url' | 'topic'
    status              TEXT NOT NULL DEFAULT 'pending', -- pending | processing | done | failed
    generated_post_ids  UUID[],                     -- linkedin_posts generados
    error_message       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER set_updated_at BEFORE UPDATE ON on_demand_requests
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- ============================================================
-- DATOS SEED: fuentes no-RSS de ejemplo
-- Las fuentes RSS se sincronizan automáticamente desde config/sources.yml
-- ============================================================

INSERT INTO sources (name, type, config, fetch_interval) VALUES
    ('Gmail Media Hub Label',   'gmail',  '{"label": "media-hub"}', '6 hours'),
    ('Google Drive PDFs',       'gdrive', '{"folder_id": "REEMPLAZAR_CON_TU_FOLDER_ID"}', '24 hours');
