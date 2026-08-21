-- Persist the versioned M4 evidence-asset manifest consumed by M5.
-- Legacy found_assets_json remains for UI/backward compatibility.

ALTER TABLE audit_result
    ADD COLUMN evidence_assets_json TEXT NOT NULL DEFAULT '[]';
