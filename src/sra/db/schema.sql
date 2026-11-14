-- Site Reliability Assessment Bot schema (PostgreSQL 15+; pgvector optional but recommended).
-- Applied idempotently on startup (sra.store.PgStore.init), like COG's schema: tables are created on first use.
-- Every table and index is prefixed sra_ so the service can share a CAP-provisioned database with other
-- applications (COG's tables are prefixed cog_). The pgvector extension is enabled by PgStore.init when the
-- database user may do so; on Cloud SQL an administrator may need to run CREATE EXTENSION vector once.

CREATE TABLE IF NOT EXISTS sra_applications (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    cmdb_id     text NOT NULL DEFAULT '',
    profile     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name)
);

-- The full Assessment document (results, findings, sign-offs) is stored as JSONB and versioned by `rev`;
-- findings are also projected into their own table for reporting and cross-assessment queries.
CREATE TABLE IF NOT EXISTS sra_assessments (
    id              text PRIMARY KEY,
    application_id  text NOT NULL REFERENCES sra_applications(id),
    status          text NOT NULL DEFAULT 'draft',
    rev             integer NOT NULL DEFAULT 1,
    doc             jsonb NOT NULL,
    scorecard       jsonb,
    matrix_version  text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sra_assessments_app_idx ON sra_assessments (application_id, created_at DESC);

-- Uploaded evidence files (PgBlobs), keyed "<assessment id>/<evidence id>__<name>". The default blob store on CAP:
-- the database is the system of record, as for OIP; GCS (SRA_GCS_BUCKET) remains optional.
CREATE TABLE IF NOT EXISTS sra_blobs (
    key         text PRIMARY KEY,
    data        bytea NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sra_evidence (
    id             text NOT NULL,
    assessment_id  text NOT NULL REFERENCES sra_assessments(id) ON DELETE CASCADE,
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

-- ---------------------------------------------------------------------------------------------------------------
-- Standing knowledge store (docs/Hybrid Retrieval Architecture Overview.png, box 3; code in sra/knowledge).
-- It replaces sra_evidence_chunks, which tied every chunk to one assessment. Its content is re-derived from the
-- stored files and source systems, so the old table is simply dropped.
DROP TABLE IF EXISTS sra_evidence_chunks;

-- One row per source document or record. assessment_id is set only for files uploaded to an assessment (deleted
-- with it); ServiceNow records and the OIPReqs standards have no owner and are shared by every consumer.
CREATE TABLE IF NOT EXISTS sra_documents (
    id                 text PRIMARY KEY,                 -- DOC-<hash of source_system|source_id>
    source_system      text NOT NULL,                    -- upload | servicenow | oipreqs (appdynamics, splunk later)
    source_id          text NOT NULL,                    -- upload: <assessment>/<evidence id>; servicenow: <table>/<sys_id>
    assessment_id      text REFERENCES sra_assessments(id) ON DELETE CASCADE,
    evidence_id        text,                             -- sra_evidence.id for uploads
    title              text NOT NULL DEFAULT '',         -- file name, record number, document code
    kind               text NOT NULL DEFAULT '',         -- document | servicenow_table | scan | record | standard | ...
    doc_type           text,                             -- runbook | architecture | standard | playbook | policy | ...
    table_name         text,                             -- ServiceNow table
    application        text NOT NULL DEFAULT '',
    scope_text         text NOT NULL DEFAULT '',         -- lower-cased names this document is about (application matching)
    tags               text[] NOT NULL DEFAULT '{}',
    acl                text[] NOT NULL DEFAULT '{}',     -- users/groups/roles allowed to read; empty = no restriction
    record             jsonb,                            -- the source record (redacted), for records
    meta               jsonb NOT NULL DEFAULT '{}'::jsonb,
    blob_uri           text,                             -- the stored file, for uploads
    hint               text,
    sha256             text NOT NULL DEFAULT '',         -- content hash: an unchanged document is not re-ingested
    chunks             integer NOT NULL DEFAULT 0,
    source_updated_at  timestamptz,
    ingested_at        timestamptz NOT NULL DEFAULT now(),
    indexed_at         timestamptz,                      -- NULL = waiting for the ingestion service
    index_error        text,
    UNIQUE (source_system, source_id)
);
CREATE INDEX IF NOT EXISTS sra_documents_assessment_idx ON sra_documents (assessment_id);
CREATE INDEX IF NOT EXISTS sra_documents_pending_idx ON sra_documents (ingested_at) WHERE indexed_at IS NULL AND index_error IS NULL;
CREATE INDEX IF NOT EXISTS sra_documents_source_idx ON sra_documents (source_system, table_name);
CREATE INDEX IF NOT EXISTS sra_documents_app_idx ON sra_documents (lower(application));

-- The retrievable pieces. Keyword search: generated tsvector + GIN. Semantic search: embedding vector(384) + HNSW,
-- added by sra.store.PgStore.init when the pgvector extension is available (keyword-only otherwise).
CREATE TABLE IF NOT EXISTS sra_chunks (
    document_id      text NOT NULL REFERENCES sra_documents(id) ON DELETE CASCADE,
    n                integer NOT NULL,
    id               text NOT NULL,                      -- uploads: <evidence id>#<n> (cited in results); else <doc id>#<n>
    text             text NOT NULL,
    tsv              tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding_model  text,
    PRIMARY KEY (document_id, n)
);
CREATE INDEX IF NOT EXISTS sra_chunks_tsv_idx ON sra_chunks USING gin (tsv);

-- Links between documents: incident -> problem (problem_id), problem/incident -> change (rfc, caused_by),
-- record -> configuration item (cmdb_ci), CI -> CI (cmdb_rel_ci), document -> the service it is about (about),
-- standard -> a document it cites (references). to_id may name a document that has not arrived yet.
CREATE TABLE IF NOT EXISTS sra_relations (
    from_id  text NOT NULL REFERENCES sra_documents(id) ON DELETE CASCADE,
    to_id    text NOT NULL,
    type     text NOT NULL,
    derived  boolean NOT NULL DEFAULT false,             -- computed by matching names, not stated by the source
    PRIMARY KEY (from_id, to_id, type)
);
CREATE INDEX IF NOT EXISTS sra_relations_to_idx ON sra_relations (to_id);

-- Where each connector stream stopped, for incremental sync.
CREATE TABLE IF NOT EXISTS sra_sync_state (
    source     text NOT NULL,
    stream     text NOT NULL,
    watermark  text,
    last_run   timestamptz,
    status     text NOT NULL DEFAULT '',
    records    integer NOT NULL DEFAULT 0,
    error      text,
    PRIMARY KEY (source, stream)
);

CREATE TABLE IF NOT EXISTS sra_findings (
    id             text NOT NULL,
    assessment_id  text NOT NULL REFERENCES sra_assessments(id) ON DELETE CASCADE,
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
CREATE INDEX IF NOT EXISTS sra_findings_open_idx ON sra_findings (severity, status);

CREATE TABLE IF NOT EXISTS sra_scoring_matrix_versions (
    version     text PRIMARY KEY,
    doc         jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sra_audit_log (
    id             bigserial PRIMARY KEY,
    assessment_id  text NOT NULL,
    actor          text NOT NULL,
    action         text NOT NULL,
    target         text NOT NULL DEFAULT '',
    before         jsonb,
    after          jsonb,
    at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sra_audit_log_assessment_idx ON sra_audit_log (assessment_id, at);

-- Transactional outbox: rows are written in the same transaction as the state change and
-- relayed to Kafka by the worker (sra.events.relay), so there is never a dual write.
CREATE TABLE IF NOT EXISTS sra_outbox (
    id            bigserial PRIMARY KEY,
    topic         text NOT NULL,
    key           text NOT NULL,
    payload       jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    published_at  timestamptz
);
CREATE INDEX IF NOT EXISTS sra_outbox_pending_idx ON sra_outbox (id) WHERE published_at IS NULL;

-- Human corrections to AI results, kept as training data for later LoRA fine-tuning.
CREATE TABLE IF NOT EXISTS sra_feedback (
    id             bigserial PRIMARY KEY,
    assessment_id  text NOT NULL,
    criterion_id   text NOT NULL,
    ai_result      jsonb,
    human_result   jsonb NOT NULL,
    at             timestamptz NOT NULL DEFAULT now()
);
-- kind: confirm | override | fill | revise. application and excerpts are filled in when the assessment is deleted,
-- so the decision stays usable as training data without the assessment.
ALTER TABLE sra_feedback ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'override';
ALTER TABLE sra_feedback ADD COLUMN IF NOT EXISTS application text NOT NULL DEFAULT '';
ALTER TABLE sra_feedback ADD COLUMN IF NOT EXISTS excerpts jsonb;

-- Progress of the latest automated run per assessment (written by the API or the worker, read by the UI).
CREATE TABLE IF NOT EXISTS sra_jobs (
  assessment_id TEXT PRIMARY KEY,
  state         JSONB NOT NULL,
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE sra_jobs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT false;
