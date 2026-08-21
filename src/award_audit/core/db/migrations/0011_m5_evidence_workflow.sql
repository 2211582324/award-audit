-- M5 evidence workflow: separate durable case history from one execution attempt.
CREATE TABLE IF NOT EXISTS audit_attempt (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id               INTEGER NOT NULL REFERENCES audit_case(id),
    sequence              INTEGER NOT NULL,
    kind                  TEXT NOT NULL DEFAULT 'initial',
    supplement_request    TEXT NOT NULL DEFAULT '',
    status                TEXT NOT NULL DEFAULT 'running',
    phase                 TEXT NOT NULL DEFAULT 'scope_confirmation',
    budget_limits_json    TEXT NOT NULL DEFAULT '{}',
    budget_usage_json     TEXT NOT NULL DEFAULT '{}',
    step_count            INTEGER NOT NULL DEFAULT 0,
    token_used            INTEGER NOT NULL DEFAULT 0,
    elapsed_ms            INTEGER NOT NULL DEFAULT 0,
    stop_reason           TEXT NOT NULL DEFAULT '',
    verifier_status       TEXT NOT NULL DEFAULT 'pending',
    conclusion_readiness  TEXT NOT NULL DEFAULT 'incomplete',
    blockers_json         TEXT NOT NULL DEFAULT '[]',
    started_at            TEXT NOT NULL,
    finished_at           TEXT NOT NULL DEFAULT '',
    updated_at            TEXT NOT NULL,
    UNIQUE(case_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_audit_attempt_case
    ON audit_attempt(case_id, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_attempt_running
    ON audit_attempt(case_id) WHERE status='running';

ALTER TABLE tool_trace ADD COLUMN attempt_id INTEGER REFERENCES audit_attempt(id);
ALTER TABLE evidence_artifact ADD COLUMN attempt_id INTEGER REFERENCES audit_attempt(id);
ALTER TABLE verification_report ADD COLUMN attempt_id INTEGER REFERENCES audit_attempt(id);

CREATE TABLE IF NOT EXISTS evidence_source (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id            INTEGER NOT NULL REFERENCES audit_case(id),
    first_attempt_id   INTEGER REFERENCES audit_attempt(id),
    last_attempt_id    INTEGER REFERENCES audit_attempt(id),
    url                TEXT NOT NULL,
    canonical_url      TEXT NOT NULL,
    title              TEXT NOT NULL DEFAULT '',
    provider           TEXT NOT NULL DEFAULT '',
    authority          TEXT NOT NULL DEFAULT 'unknown',
    relevance          TEXT NOT NULL DEFAULT 'unreviewed',
    relevance_score    INTEGER NOT NULL DEFAULT 0,
    scope_json         TEXT NOT NULL DEFAULT '{}',
    status             TEXT NOT NULL DEFAULT 'discovered',
    exclusion_reason   TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE(case_id, canonical_url)
);
CREATE INDEX IF NOT EXISTS idx_evidence_source_case
    ON evidence_source(case_id, status, id);

CREATE TABLE IF NOT EXISTS evidence_group (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id            INTEGER NOT NULL REFERENCES audit_case(id),
    attempt_id         INTEGER NOT NULL REFERENCES audit_attempt(id),
    group_key          TEXT NOT NULL,
    parent_url         TEXT NOT NULL DEFAULT '',
    title              TEXT NOT NULL DEFAULT '',
    scope_json         TEXT NOT NULL DEFAULT '{}',
    status             TEXT NOT NULL DEFAULT 'collecting',
    expected_assets    INTEGER NOT NULL DEFAULT 0,
    terminal_assets    INTEGER NOT NULL DEFAULT 0,
    extracted_count    INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE(attempt_id, group_key)
);
CREATE INDEX IF NOT EXISTS idx_evidence_group_case
    ON evidence_group(case_id, attempt_id, id);

CREATE TABLE IF NOT EXISTS evidence_asset_task (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id            INTEGER NOT NULL REFERENCES audit_case(id),
    first_attempt_id   INTEGER REFERENCES audit_attempt(id),
    last_attempt_id    INTEGER REFERENCES audit_attempt(id),
    source_id          INTEGER REFERENCES evidence_source(id),
    group_id           INTEGER REFERENCES evidence_group(id),
    url                TEXT NOT NULL,
    canonical_url      TEXT NOT NULL,
    parent_url         TEXT NOT NULL DEFAULT '',
    label              TEXT NOT NULL DEFAULT '',
    kind               TEXT NOT NULL DEFAULT 'unknown',
    status             TEXT NOT NULL DEFAULT 'discovered',
    content_type       TEXT NOT NULL DEFAULT '',
    sha256             TEXT NOT NULL DEFAULT '',
    local_path         TEXT NOT NULL DEFAULT '',
    extraction_method  TEXT NOT NULL DEFAULT '',
    extracted_count    INTEGER NOT NULL DEFAULT 0,
    retry_count        INTEGER NOT NULL DEFAULT 0,
    error_code         TEXT NOT NULL DEFAULT '',
    error_message      TEXT NOT NULL DEFAULT '',
    scope_json         TEXT NOT NULL DEFAULT '{}',
    metadata_json      TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE(case_id, canonical_url)
);
CREATE INDEX IF NOT EXISTS idx_evidence_asset_task_case
    ON evidence_asset_task(case_id, status, id);

CREATE TABLE IF NOT EXISTS evidence_identity (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id            INTEGER NOT NULL REFERENCES audit_case(id),
    attempt_id         INTEGER REFERENCES audit_attempt(id),
    group_id           INTEGER REFERENCES evidence_group(id),
    origin             TEXT NOT NULL,
    identity_key       TEXT NOT NULL,
    display_value      TEXT NOT NULL,
    scope_json         TEXT NOT NULL DEFAULT '{}',
    source_ref         TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL,
    UNIQUE(case_id, attempt_id, group_id, origin, identity_key, source_ref)
);
CREATE INDEX IF NOT EXISTS idx_evidence_identity_case
    ON evidence_identity(case_id, attempt_id, origin, identity_key);

CREATE TABLE IF NOT EXISTS evidence_comparison (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id            INTEGER NOT NULL REFERENCES audit_case(id),
    attempt_id         INTEGER NOT NULL REFERENCES audit_attempt(id),
    group_id           INTEGER REFERENCES evidence_group(id),
    status             TEXT NOT NULL DEFAULT 'incomplete',
    submitted_count    INTEGER NOT NULL DEFAULT 0,
    evidence_count     INTEGER NOT NULL DEFAULT 0,
    matched_count      INTEGER NOT NULL DEFAULT 0,
    missing_json       TEXT NOT NULL DEFAULT '[]',
    extra_json         TEXT NOT NULL DEFAULT '[]',
    contradictions_json TEXT NOT NULL DEFAULT '[]',
    blockers_json      TEXT NOT NULL DEFAULT '[]',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE(attempt_id, group_id)
);
CREATE INDEX IF NOT EXISTS idx_evidence_comparison_case
    ON evidence_comparison(case_id, attempt_id, id);

-- Preserve pre-0011 executions as immutable legacy attempts.
INSERT INTO audit_attempt(
    case_id,sequence,kind,status,phase,budget_limits_json,budget_usage_json,
    step_count,token_used,elapsed_ms,stop_reason,verifier_status,
    conclusion_readiness,blockers_json,started_at,finished_at,updated_at
)
SELECT c.id,1,'legacy',
       CASE WHEN c.status='running' THEN 'interrupted' ELSE 'legacy' END,
       CASE WHEN c.status IN ('waiting_human','completed') THEN 'waiting_human' ELSE 'legacy' END,
       COALESCE(json_extract(c.budget_json,'$.limits'),'{}'),COALESCE(c.budget_json,'{}'),
       c.step_count,c.token_used,c.elapsed_ms,
       CASE WHEN c.status='running' THEN 'migration_interrupted' ELSE '' END,
       CASE WHEN EXISTS(SELECT 1 FROM verification_report v WHERE v.case_id=c.id)
            THEN 'persisted' ELSE 'missing' END,
       CASE WHEN EXISTS(SELECT 1 FROM verification_report v WHERE v.case_id=c.id)
            THEN 'ready_for_human' ELSE 'incomplete' END,
       CASE WHEN EXISTS(SELECT 1 FROM verification_report v WHERE v.case_id=c.id)
            THEN '[]' ELSE '["verifier_missing"]' END,
       c.created_at,
       CASE WHEN c.status='running' THEN '' ELSE c.updated_at END,
       c.updated_at
  FROM audit_case c
 WHERE NOT EXISTS(SELECT 1 FROM audit_attempt a WHERE a.case_id=c.id);

UPDATE tool_trace
   SET attempt_id=(SELECT MAX(a.id) FROM audit_attempt a WHERE a.case_id=tool_trace.case_id)
 WHERE attempt_id IS NULL;
UPDATE evidence_artifact
   SET attempt_id=(SELECT MAX(a.id) FROM audit_attempt a WHERE a.case_id=evidence_artifact.case_id)
 WHERE attempt_id IS NULL;
UPDATE verification_report
   SET attempt_id=(SELECT MAX(a.id) FROM audit_attempt a WHERE a.case_id=verification_report.case_id)
 WHERE attempt_id IS NULL;
