DROP INDEX IF EXISTS uq_audit_case_active_trigger;

CREATE UNIQUE INDEX uq_audit_case_active_trigger
    ON audit_case(batch_id, resource_code, year, trigger_key)
    WHERE status IN ('queued', 'running', 'waiting_human');
