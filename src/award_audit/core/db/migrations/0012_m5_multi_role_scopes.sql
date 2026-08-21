-- M5 v3: one audit case contains independent business-role scopes.
CREATE TABLE IF NOT EXISTS audit_scope (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id                  INTEGER NOT NULL REFERENCES audit_case(id),
    scope_key                TEXT NOT NULL,
    role_type                TEXT NOT NULL,
    role_label               TEXT NOT NULL DEFAULT '',
    required                 INTEGER NOT NULL DEFAULT 1,
    identity_version         TEXT NOT NULL DEFAULT 'identity-v2',
    profile_json             TEXT NOT NULL DEFAULT '{}',
    business_scope_json      TEXT NOT NULL DEFAULT '{}',
    submitted_row_count      INTEGER NOT NULL DEFAULT 0,
    submitted_identity_count INTEGER NOT NULL DEFAULT 0,
    unidentified_row_count   INTEGER NOT NULL DEFAULT 0,
    submitted_identities_json TEXT NOT NULL DEFAULT '{}',
    status                   TEXT NOT NULL DEFAULT 'pending',
    blockers_json            TEXT NOT NULL DEFAULT '[]',
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    UNIQUE(case_id, scope_key)
);
CREATE INDEX IF NOT EXISTS idx_audit_scope_case
    ON audit_scope(case_id, required, id);

ALTER TABLE evidence_source ADD COLUMN scope_id INTEGER REFERENCES audit_scope(id);
ALTER TABLE evidence_group ADD COLUMN scope_id INTEGER REFERENCES audit_scope(id);
ALTER TABLE evidence_asset_task ADD COLUMN scope_id INTEGER REFERENCES audit_scope(id);
ALTER TABLE evidence_identity ADD COLUMN scope_id INTEGER REFERENCES audit_scope(id);
ALTER TABLE evidence_comparison ADD COLUMN legacy INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS evidence_source_scope (
    source_id INTEGER NOT NULL REFERENCES evidence_source(id),
    scope_id  INTEGER NOT NULL REFERENCES audit_scope(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(source_id, scope_id)
);

CREATE TABLE IF NOT EXISTS evidence_scope_comparison (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id                  INTEGER NOT NULL REFERENCES audit_case(id),
    attempt_id               INTEGER NOT NULL REFERENCES audit_attempt(id),
    scope_id                 INTEGER NOT NULL REFERENCES audit_scope(id),
    status                   TEXT NOT NULL DEFAULT 'incomplete',
    evidence_complete        INTEGER NOT NULL DEFAULT 0,
    comparison_result        TEXT NOT NULL DEFAULT 'not_compared',
    submitted_row_count      INTEGER NOT NULL DEFAULT 0,
    submitted_identity_count INTEGER NOT NULL DEFAULT 0,
    evidence_identity_count  INTEGER NOT NULL DEFAULT 0,
    matched_count            INTEGER NOT NULL DEFAULT 0,
    missing_json             TEXT NOT NULL DEFAULT '[]',
    extra_json               TEXT NOT NULL DEFAULT '[]',
    conflicts_json           TEXT NOT NULL DEFAULT '[]',
    blockers_json            TEXT NOT NULL DEFAULT '[]',
    verifier_json            TEXT NOT NULL DEFAULT '{}',
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    UNIQUE(attempt_id, scope_id)
);
CREATE INDEX IF NOT EXISTS idx_scope_comparison_case
    ON evidence_scope_comparison(case_id, attempt_id, scope_id);

CREATE TABLE IF NOT EXISTS audit_attempt_budget (
    attempt_id       INTEGER NOT NULL REFERENCES audit_attempt(id),
    budget_kind      TEXT NOT NULL,
    limit_value      INTEGER NOT NULL DEFAULT 0,
    used_value       INTEGER NOT NULL DEFAULT 0,
    metadata_json    TEXT NOT NULL DEFAULT '{}',
    updated_at       TEXT NOT NULL,
    PRIMARY KEY(attempt_id, budget_kind)
);
