-- Persist the identity-construction contract used by each immutable M4 result.

ALTER TABLE audit_result
    ADD COLUMN identity_version TEXT NOT NULL DEFAULT 'identity-v1';
