CREATE TABLE audit_job (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kind              TEXT    NOT NULL,
    batch_id          INTEGER REFERENCES import_batch(id),
    case_id           INTEGER REFERENCES audit_case(id),
    status            TEXT    NOT NULL DEFAULT 'queued',
    input_json        TEXT    NOT NULL DEFAULT '{}',
    progress          INTEGER NOT NULL DEFAULT 0,
    progress_message  TEXT    NOT NULL DEFAULT '',
    result_json       TEXT    NOT NULL DEFAULT '{}',
    error_code        TEXT    NOT NULL DEFAULT '',
    error_message     TEXT    NOT NULL DEFAULT '',
    attempt           INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 1,
    lease_owner       TEXT    NOT NULL DEFAULT '',
    lease_expires_at  TEXT    NOT NULL DEFAULT '',
    state_version     INTEGER NOT NULL DEFAULT 1,
    created_by        TEXT    NOT NULL,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    started_at        TEXT    NOT NULL DEFAULT '',
    finished_at       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX idx_audit_job_queue
    ON audit_job(status, created_at, id);
CREATE INDEX idx_audit_job_batch
    ON audit_job(batch_id, id);

CREATE TABLE audit_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type    TEXT    NOT NULL,
    topic         TEXT    NOT NULL DEFAULT 'global',
    job_id        INTEGER REFERENCES audit_job(id),
    case_id       INTEGER REFERENCES audit_case(id),
    batch_id      INTEGER REFERENCES import_batch(id),
    payload_json  TEXT    NOT NULL DEFAULT '{}',
    created_at    TEXT    NOT NULL
);
CREATE INDEX idx_audit_event_cursor ON audit_event(id);
CREATE INDEX idx_audit_event_case ON audit_event(case_id, id);
CREATE INDEX idx_audit_event_job ON audit_event(job_id, id);

CREATE TABLE human_action_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reviewer        TEXT    NOT NULL,
    action          TEXT    NOT NULL,
    target_type     TEXT    NOT NULL,
    target_id       INTEGER NOT NULL,
    before_version  INTEGER NOT NULL DEFAULT 0,
    after_version   INTEGER NOT NULL DEFAULT 0,
    summary         TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL
);
CREATE INDEX idx_human_action_target
    ON human_action_log(target_type, target_id, id);
