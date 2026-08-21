-- M5 unified multi-asset routing and submitted-row conservation.
CREATE TABLE IF NOT EXISTS audit_scope_assignment (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id        INTEGER NOT NULL REFERENCES audit_case(id),
    source_path    TEXT NOT NULL DEFAULT '',
    sheet_name     TEXT NOT NULL DEFAULT '',
    row_number     INTEGER NOT NULL,
    category       TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL,
    scope_keys_json TEXT NOT NULL DEFAULT '[]',
    scope_ids_json TEXT NOT NULL DEFAULT '[]',
    reasons_json   TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE(case_id, source_path, sheet_name, row_number)
);
CREATE INDEX IF NOT EXISTS idx_audit_scope_assignment_case
    ON audit_scope_assignment(case_id, status, id);

CREATE TABLE IF NOT EXISTS evidence_asset_scope (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id         INTEGER NOT NULL REFERENCES evidence_asset_task(id),
    scope_id         INTEGER REFERENCES audit_scope(id),
    group_id         INTEGER REFERENCES evidence_group(id),
    subunit_type     TEXT NOT NULL DEFAULT 'document',
    selector_json    TEXT NOT NULL DEFAULT '{}',
    identity_fields_json TEXT NOT NULL DEFAULT '[]',
    route_source     TEXT NOT NULL DEFAULT 'exact_rule',
    confidence       REAL NOT NULL DEFAULT 0,
    route_status     TEXT NOT NULL DEFAULT 'pending',
    processing_status TEXT NOT NULL DEFAULT 'pending',
    reason           TEXT NOT NULL DEFAULT '',
    extracted_count  INTEGER NOT NULL DEFAULT 0,
    blockers_json    TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_evidence_asset_scope_route
    ON evidence_asset_scope(asset_id, IFNULL(scope_id, 0), subunit_type, selector_json);
CREATE INDEX IF NOT EXISTS idx_evidence_asset_scope_scope
    ON evidence_asset_scope(scope_id, route_status, processing_status, id);
