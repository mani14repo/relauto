-- Site Reliability Assessment Bot schema (PostgreSQL 15+ with pgvector).
-- Applied idempotently on startup (sra.store.PgStore.init). Cloud SQL: enable the `vector` extension first.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS applications (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    cmdb_id     text NOT NULL DEFAULT '',
    profile     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name)
);

-- The full Assessment document (results, findings, sign-offs) is stored as JSONB and versioned by `rev`;
-- findings are also projected into their own table for reporting and cross-assessment queries.
CREATE TABLE IF NOT EXISTS assessments (
    id              text PRIMARY KEY,
    application_id  text NOT NULL REFERENCES applications(id),
    status          text NOT NULL DEFAULT 'draft',
    rev             integer NOT NULL DEFAULT 1,
    doc             jsonb NOT NULL,
    scorecard       jsonb,
    matrix_version  text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assessments_app_idx ON assessments (application_id, created_at DESC);

CREATE TABLE IF NOT EXISTS evidence (
    id             text NOT NULL,
    assessment_id  text NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    name           text NOT NULL,
    kind           text NOT NULL DEFAULT '',
    doc_type       text,
    table_name     text,
    hint           text,
    sha256         text NOT NULL,
    blob_uri       text NOT NULL,
    meta           jsonb NOT NULL DEFAULT '{}'::jsonb,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (assessment_id, id)
);

CREATE TABLE IF NOT EXISTS evidence_chunks (
    id             text NOT NULL,
    assessment_id  text NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    evidence_id    text NOT NULL,
    source         text NOT NULL,
    text           text NOT NULL,
    tsv            tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding      vector(384),
    PRIMARY KEY (assessment_id, id)
);
CREATE INDEX IF NOT EXISTS evidence_chunks_tsv_idx ON evidence_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS evidence_chunks_vec_idx ON evidence_chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS findings (
    id             text NOT NULL,
    assessment_id  text NOT NULL REFERENCES assessments(id) ON DELETE CASCADE,
    type           text NOT NULL,
    severity       text NOT NULL,
    status         text NOT NULL,
    title          text NOT NULL,
    criterion_ids  text[] NOT NULL DEFAULT '{}',
    rubric_ids     text[] NOT NULL DEFAULT '{}',
    detector       text NOT NULL DEFAULT '',
    due_date       date,
    owner          text NOT NULL DEFAULT '',
    doc            jsonb NOT NULL,
    PRIMARY KEY (assessment_id, id)
);
CREATE INDEX IF NOT EXISTS findings_open_idx ON findings (severity, status);

CREATE TABLE IF NOT EXISTS scoring_matrix_versions (
    version     text PRIMARY KEY,
    doc         jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id             bigserial PRIMARY KEY,
    assessment_id  text NOT NULL,
    actor          text NOT NULL,
    action         text NOT NULL,
    target         text NOT NULL DEFAULT '',
    before         jsonb,
    after          jsonb,
    at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_assessment_idx ON audit_log (assessment_id, at);

-- Transactional outbox: rows are written in the same transaction as the state change and
-- relayed to Kafka by the worker (sra.events.relay), so there is never a dual write.
CREATE TABLE IF NOT EXISTS outbox (
    id            bigserial PRIMARY KEY,
    topic         text NOT NULL,
    key           text NOT NULL,
    payload       jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    published_at  timestamptz
);
CREATE INDEX IF NOT EXISTS outbox_pending_idx ON outbox (id) WHERE published_at IS NULL;

-- Human corrections to AI results, kept as training data for later LoRA fine-tuning.
CREATE TABLE IF NOT EXISTS feedback (
    id             bigserial PRIMARY KEY,
    assessment_id  text NOT NULL,
    criterion_id   text NOT NULL,
    ai_result      jsonb,
    human_result   jsonb NOT NULL,
    at             timestamptz NOT NULL DEFAULT now()
);
