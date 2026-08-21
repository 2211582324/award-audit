CREATE TABLE IF NOT EXISTS audit_case (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id                 INTEGER NOT NULL REFERENCES import_batch(id),
    resource_code            TEXT    NOT NULL,
    award_name               TEXT    NOT NULL DEFAULT '',
    year                     TEXT    NOT NULL DEFAULT '',
    trigger_key              TEXT    NOT NULL,
    trigger_codes_json       TEXT    NOT NULL DEFAULT '[]',
    objective                TEXT    NOT NULL,
    submitted_summary_json   TEXT    NOT NULL DEFAULT '{}',
    known_urls_json          TEXT    NOT NULL DEFAULT '[]',
    retrieved_memories_json  TEXT    NOT NULL DEFAULT '[]',
    open_questions_json      TEXT    NOT NULL DEFAULT '[]',
    budget_json              TEXT    NOT NULL DEFAULT '{}',
    step_count               INTEGER NOT NULL DEFAULT 0,
    token_used               INTEGER NOT NULL DEFAULT 0,
    elapsed_ms               INTEGER NOT NULL DEFAULT 0,
    status                   TEXT    NOT NULL DEFAULT 'queued',
    recommendation           TEXT    NOT NULL DEFAULT '',
    confidence               TEXT    NOT NULL DEFAULT 'low',
    reason_codes_json        TEXT    NOT NULL DEFAULT '[]',
    last_action_json         TEXT    NOT NULL DEFAULT '{}',
    last_error               TEXT    NOT NULL DEFAULT '',
    pending_supplement       TEXT    NOT NULL DEFAULT '',
    state_version            INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT    NOT NULL,
    updated_at               TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_case_batch_status
    ON audit_case(batch_id, status, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_case_active_trigger
    ON audit_case(batch_id, resource_code, trigger_key)
    WHERE status IN ('queued', 'running', 'waiting_human');

CREATE TABLE IF NOT EXISTS tool_trace (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id             INTEGER NOT NULL REFERENCES audit_case(id),
    call_id             TEXT    NOT NULL,
    tool_name           TEXT    NOT NULL,
    started_at          TEXT    NOT NULL,
    finished_at         TEXT    NOT NULL,
    duration_ms         INTEGER NOT NULL DEFAULT 0,
    input_summary_json  TEXT    NOT NULL DEFAULT '{}',
    output_summary_json TEXT    NOT NULL DEFAULT '{}',
    ok                  INTEGER NOT NULL DEFAULT 0,
    error_code          TEXT    NOT NULL DEFAULT '',
    UNIQUE(case_id, call_id)
);
CREATE INDEX IF NOT EXISTS idx_tool_trace_case ON tool_trace(case_id, id);

CREATE TABLE IF NOT EXISTS evidence_artifact (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id       INTEGER NOT NULL REFERENCES audit_case(id),
    kind          TEXT    NOT NULL,
    source_url    TEXT    NOT NULL,
    local_path    TEXT    NOT NULL,
    content_type  TEXT    NOT NULL,
    sha256        TEXT    NOT NULL,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    fetched_at    TEXT    NOT NULL,
    metadata_json TEXT    NOT NULL DEFAULT '{}',
    UNIQUE(case_id, sha256, local_path)
);
CREATE INDEX IF NOT EXISTS idx_evidence_artifact_case
    ON evidence_artifact(case_id, id);
